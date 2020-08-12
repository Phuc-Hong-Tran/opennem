import logging
import os

from sqlalchemy import create_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from sqlalchemy.sql.expression import Insert

from opennem.settings import get_database_host, get_mysql_host

DeclarativeBase = declarative_base()

logger = logging.getLogger(__name__)


@compiles(Insert, "postgresql")
def compile_upsert(insert_stmt, compiler, **kwargs):
    """
    converts every SQL insert to an upsert  i.e;
    INSERT INTO test (foo, bar) VALUES (1, 'a')
    becomes:
    INSERT INTO test (foo, bar) VALUES (1, 'a') ON CONFLICT(foo) DO UPDATE SET (bar = EXCLUDED.bar)
    (assuming foo is a primary key)


    :param insert_stmt: Original insert statement
    :param compiler: SQL Compiler
    :param kwargs: optional arguments
    :return: upsert statement
    """
    pk = insert_stmt.table.primary_key

    from pprint import pprint

    # @TODO support unique columns in UPSERT

    insert = compiler.visit_insert(insert_stmt, **kwargs)
    ondup = f'ON CONFLICT ({",".join(c.name for c in pk)}) DO UPDATE SET'
    updates = ", ".join(
        f"{c.name}=EXCLUDED.{c.name}"
        for c in insert_stmt.table.columns
        if c.name not in ["created_at"]
    )

    # @TODO Improve this - currently works for all of our queries
    # but it isn't really bulletproof

    if " RETURNING " in insert:
        pos = insert.find(" RETURNING ")
        returning = insert[pos:]
        insert = insert[:pos]
    else:
        returning = ""

    upsert = " ".join((insert, ondup, updates, returning))

    return upsert


def db_connect(db_name=None, debug=False):
    """
    Performs database connection using database settings from settings.py.
    Returns sqlalchemy engine instance
    """
    db_conn_str = get_database_host()

    try:
        e = create_engine(
            db_conn_str,
            echo=debug,
            pool_size=10,
            pool_timeout=60,
            connect_args={"check_same_thread": False},
        )
        return e
    except Exception as e:
        logger.error("Could not connect to database: {}".format(e))
        return
