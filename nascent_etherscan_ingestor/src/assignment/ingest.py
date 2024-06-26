import requests
import typer
import threading
import re
import time
import queue
from datetime import datetime, timezone
from sqlalchemy.exc import IntegrityError
from concurrent.futures import ThreadPoolExecutor, as_completed
from src.assignment.config import (CONFIG, RECORD_RETRIEVAL_LIMIT, API_KEY, PRODUCER_THREAD_COUNT,
                                   CONSUMER_THREAD_COUNT, DEV_STEP, SEMAPHOR_THREAD_COUNT, SAVE_BATCH_LIMIT,
                                   DEFAULT_ADDRESS, DEV_MODE, DEV_MODE_ENDING_MULTIPLE, DEV_PRODUCER_THREAD_COUNT,
                                   API_RATE_LIMIT_DELAY, BASE_BLOCK_ATTEMPT, BLOCK_ATTEMPTS, TEST_MODE,
                                   TEST_MODE_STARTING_BLOCK, TEST_MODE_END_BLOCK)
from src.assignment.db import init_db
from src.assignment.logger import logger
from src.assignment.models import Address, Transaction
from src.assignment.config import DATABASE_URI

app = typer.Typer()

api_call_semaphore = threading.Semaphore(SEMAPHOR_THREAD_COUNT)
Session = init_db(DATABASE_URI)


def __log_thread_error(thread_id, current_block, ending_block):
    error_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    log_msg = f"Error at {error_time}, Thread ID: {thread_id}, Current Block: {current_block}, Ending Block: {ending_block}"
    logger.error(log_msg)


def __log_with_thread_id(message, thread_id):
    full_message = f"thread_id: {thread_id}. Message: {message}"
    logger.info(full_message)


def __call_etherscan(address, thread_id=None, startblock=0, endblock=99999999, sort="asc", retries=4,
                     delay=API_RATE_LIMIT_DELAY):
    api_url = (
        "https://api.etherscan.io/api?module=account&action=txlistinternal&"
        f"address={address}&startblock={startblock}&endblock={endblock}&"
        f"page=1&offset={RECORD_RETRIEVAL_LIMIT}&sort={sort}&apikey={API_KEY}"
    )

    for attempt in range(retries):
        with api_call_semaphore:
            __log_with_thread_id(
                f"Calling Etherscan API with current_block: {startblock}, ending_block: {endblock}, thread_id: {thread_id}",
                thread_id)
            response = requests.get(api_url)
            data = response.json()
            time.sleep(delay)

        if data['result'] != 'Max rate limit reached':
            return data

    __log_thread_error(thread_id, startblock, endblock)
    raise Exception("Max rate limit reached after retrying")


def __get_or_create_address(address):
    session = Session()
    try:
        addr_obj = session.query(Address).filter_by(address=address).one_or_none()

        if not addr_obj:
            addr_obj = Address(address=address)
            session.add(addr_obj)
            session.commit()

        return addr_obj.id
    except IntegrityError:
        session.rollback()
        addr_obj = session.query(Address).filter_by(address=address).one()

        return addr_obj.id
    except Exception as e:
        logger.error(f"An error occurred: {e}")
        session.rollback()
        return None
    finally:
        session.close()


def __save(session, transactions_to_batch, thread_id):
    try:
        __log_with_thread_id(f"About to save... {len(transactions_to_batch)} records.", "Consumer_thread_" + str(thread_id))
        session.bulk_save_objects(transactions_to_batch)
        session.commit()
        transactions_to_batch.clear()
    except IntegrityError as e:
        session.rollback()
        logger.error(f"Duplicate key error occurred while inserting transactions: {e}")
        match = re.search(r'\(hash\)=\((.*?)\)', str(e))
        if match:
            conflicting_hash = match.group(1)
            logger.error(f"Conflict detected for hash: {conflicting_hash}")
            conflicting_transactions = [tx for tx in transactions_to_batch if tx.hash == conflicting_hash]
            for tx in conflicting_transactions:
                logger.error(f"Conflicting transaction: {tx.hash}")
            existing_transaction = session.query(Transaction).filter_by(hash=conflicting_hash).first()
            if existing_transaction:
                logger.error(
                    "Existing transaction in database that caused the conflict: {}".format(existing_transaction))
    except Exception as e:
        __log_with_thread_id(f"An unexpected error occurred while inserting transactions: {e}",
                             "Consumer_thread_" + str(thread_id))
        session.rollback()


def __block_ranges(start, end, step):
    current = start
    while current <= end:
        yield current, min(current + step - 1, end)
        current += step


def call_api_and_produce(address, starting_block, ending_block, transaction_queue, thread_id):
    """
    Ingest data into the database from the API.
    - call_api_and_produce from the api using specific startblock and endblock and an offset of 10000
    (the current etherscan limit)
    - if the records are 10000 or more (a sign that there are more than 10000 records in between the
    startblock and the endblock), we dynamically reduce the endblock
    - if fewer than 10000 records, we batch and save to database
    """
    current_block = starting_block
    block_window_amount = BASE_BLOCK_ATTEMPT if not DEV_MODE else DEV_STEP
    try:
        while current_block <= ending_block:
            __log_with_thread_id(
                f"New Loop. current_block: {current_block}, ending_block: {ending_block}", thread_id
            )
            tentative_end_block = min(current_block + BASE_BLOCK_ATTEMPT, ending_block)

            data = __call_etherscan(address, thread_id, startblock=current_block, endblock=tentative_end_block)

            if not data["result"]:
                __log_with_thread_id(
                    f"No transactions found for address: {address}, current_block: {current_block}, "
                    f"ending_block: {tentative_end_block}",
                    thread_id
                )
                current_block = tentative_end_block + 1
                continue

            block_attempt_index = 0

            while len(data.get("result", [])) >= RECORD_RETRIEVAL_LIMIT:  # should really never be > but just in case...
                __log_with_thread_id(
                    f"Retrieved too many records with {block_window_amount}. "
                    f"Retrieved {len(data.get("result", []))} records.",
                    thread_id
                )
                block_window_amount = BLOCK_ATTEMPTS[block_attempt_index] if block_attempt_index < len(
                    BLOCK_ATTEMPTS) else 20
                tentative_end_block = min(current_block + block_window_amount, ending_block)
                __log_with_thread_id(
                    f"Adjusting block range to {block_window_amount}, new end block: {tentative_end_block}",
                    thread_id
                )
                data = __call_etherscan(address, thread_id, startblock=current_block, endblock=tentative_end_block)
                block_attempt_index += 1

            __log_with_thread_id(
                f"Etherscan API call SUCCESS! Retrieved {len(data['result'])} records with a block window "
                f"size of {block_window_amount}",
                thread_id
            )

            for tx in data["result"]:
                if int(tx["value"]) > 0 and tx["isError"] != "1":
                    transaction_queue.put(tx)

            if data["result"]:
                __log_with_thread_id(f"Changing current block from {current_block}...", thread_id)
                current_block = tentative_end_block + 1
                __log_with_thread_id(f"...to new current block {current_block}", thread_id)
                block_window_amount = BASE_BLOCK_ATTEMPT

    except Exception as e:
        logger.error(f"Failed inside call_api_and_produce data with error: {e}")
        raise
    finally:
        __log_with_thread_id(
            f"Reached endblock. current_block: {current_block}, end_block: {ending_block}. Killing thread",
            thread_id)


def consume(transaction_queue, producers_all_done_event, thread_id):
    __log_with_thread_id("CONSUMER STARTING!", "Consumer_thread_" + str(thread_id))
    session = Session()
    transactions_to_batch = []

    try:
        # run until all producers are done and the queue is empty
        while not producers_all_done_event.is_set() or not transaction_queue.empty():
            try:
                transaction = transaction_queue.get(timeout=5)
                from_address_id = __get_or_create_address(transaction["from"])
                to_address_id = __get_or_create_address(transaction["to"])

                transaction = Transaction(
                    block_number=int(transaction["blockNumber"]),
                    time_stamp=datetime.fromtimestamp(int(transaction["timeStamp"]), timezone.utc),
                    hash=transaction["hash"],
                    from_address_id=from_address_id,
                    to_address_id=to_address_id,
                    value=int(transaction["value"]),
                    gas=int(transaction["gas"]),
                    gas_used=int(transaction["gasUsed"]),
                    is_error=int(transaction["isError"]),
                )
                transactions_to_batch.append(transaction)
                if len(transactions_to_batch) >= SAVE_BATCH_LIMIT:
                    __save(session, transactions_to_batch, thread_id)
                    transactions_to_batch.clear()
                    __log_with_thread_id(
                        f"Batch saved. Current transaction queue size: {transaction_queue.qsize()}. "
                        f"Current db size: {session.query(Transaction).count()}",
                        "Consumer_thread_" + str(thread_id)
                    )
            except queue.Empty:
                if producers_all_done_event.is_set():
                    __log_with_thread_id("Producers have finished; no more transactions "
                                         "are expected.", "Consumer_thread_" + str(thread_id)
                                         )
                    break
                __log_with_thread_id(
                    "Queue is empty, waiting for new transactions...", "Consumer_thread_" + str(thread_id)
                )
                continue

        if transactions_to_batch:  # handle any last transactions... say the producers end but the queue is not empty.
            __save(session, transactions_to_batch, thread_id)
            __log_with_thread_id(f"Final batch saved. Batch size: {len(transactions_to_batch)}", thread_id)
    except Exception as e:
        __log_with_thread_id(f"Consumer shutting down due to error: {e}", thread_id)
    finally:
        session.close()
        __log_with_thread_id("Session closed and consumer shutdown.", thread_id)


@app.command()
def start():
    logger.info(f"CONFIG: {CONFIG}")
    data = __call_etherscan(DEFAULT_ADDRESS, sort="asc")
    starting_block = int(data["result"][0]["blockNumber"]) if not TEST_MODE else TEST_MODE_STARTING_BLOCK
    logger.info(f"Address starting_block: {starting_block}")

    if DEV_MODE:
        logger.info("IN DEV MODE, CONTRIVED END BLOCK")
        ending_block = starting_block + DEV_PRODUCER_THREAD_COUNT * DEV_MODE_ENDING_MULTIPLE
    elif TEST_MODE:
        logger.info("IN TEST MODE, CONTRIVED END BLOCK")
        ending_block = int(TEST_MODE_END_BLOCK)
    else:
        data = __call_etherscan(DEFAULT_ADDRESS, sort="desc")
        ending_block = int(data["result"][0]["blockNumber"])
    logger.info(f"Address ending_block: {ending_block}")

    queue_for_transactions = queue.Queue()
    producers_all_done_event = threading.Event()
    consumer_futures = []

    block_generator = __block_ranges(starting_block, ending_block,
                                     BASE_BLOCK_ATTEMPT if not DEV_MODE else DEV_STEP)

    with ThreadPoolExecutor(
            max_workers=PRODUCER_THREAD_COUNT if not DEV_MODE else DEV_PRODUCER_THREAD_COUNT) as executor:
        futures = []
        thread_id = 0

        for consumer_thread_id in range(CONSUMER_THREAD_COUNT):
            consumer_futures.append(
                executor.submit(consume, queue_for_transactions, producers_all_done_event, consumer_thread_id)
            )

        for _ in range(PRODUCER_THREAD_COUNT):
            try:
                new_range = next(block_generator)
                futures.append(
                    executor.submit(call_api_and_produce, DEFAULT_ADDRESS, *new_range, queue_for_transactions,
                                    thread_id)
                )
                thread_id += 1
            except StopIteration:
                break

        while futures:
            for future in as_completed(futures):
                futures.remove(future)
                try:
                    new_range = next(block_generator)
                    futures.append(
                        executor.submit(call_api_and_produce, DEFAULT_ADDRESS, *new_range, queue_for_transactions,
                                        thread_id)
                    )
                    thread_id += 1
                except StopIteration:
                    break

        producers_all_done_event.set()
        logger.info("All producers have finished producing.")

        for consumer_future in consumer_futures:
            consumer_result = consumer_future.result()  # This ensures that the consumer has processed all items
        logger.info(f"Consumer has finished processing all items. {consumer_result}")

    logger.info("Executor shutdown of consumer and producer complete. Jeff Wan signing off.")


if __name__ == "__main__":
    start()
