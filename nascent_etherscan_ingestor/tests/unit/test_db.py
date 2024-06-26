import pytest
from src.assignment.models import Base
from src.assignment.db import init_db


@pytest.fixture
def mock_create_engine(mocker):
    mock_created_engine = mocker.Mock()
    mock_create_engine = mocker.patch('src.assignment.db.create_engine', return_value=mock_created_engine)

    return mock_created_engine, mock_create_engine

@pytest.fixture
def mock_create_all(mocker):
    return mocker.patch.object(Base.metadata, 'create_all')


@pytest.fixture
def mock_session(mocker):
    mock_session = mocker.Mock()
    mock_scoped_session = mocker.patch('src.assignment.db.scoped_session', return_value=mock_session)

    return mock_session, mock_scoped_session


def test_init_db_creates_engine_and_tables(mock_create_engine, mock_create_all):
    mock_created_engine, mock_create_engine = mock_create_engine
    test_url = 'test_url'

    init_db(test_url)

    mock_create_engine.assert_called_once_with(test_url)
    mock_create_all.assert_called_once_with(mock_created_engine)


def test_init_db_returns_scoped_session(mock_create_engine, mock_create_all, mock_session):
    test_url = 'test_url'
    expected, _ = mock_session

    actual = init_db(test_url)

    assert actual == expected


def test_scoped_session_called(mocker, mock_session, mock_create_engine):
    mock_session_maker_return = mocker.Mock()
    mocker.patch('src.assignment.db.sessionmaker', return_value=mock_session_maker_return)
    _, mock_scoped_session = mock_session
    test_url = 'test_url'

    init_db(test_url)

    mock_scoped_session.assert_called_once_with(mock_session_maker_return)
