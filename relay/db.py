"""Persist only credential hashes and device-scoped permissions, never job data."""
import hashlib
import secrets
import time

from sqlalchemy import Boolean, Float, ForeignKey, Index, String, create_engine, func, inspect, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool


def digest(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def token() -> str:
    return secrets.token_urlsafe(32)


class Base(DeclarativeBase):
    pass


class Device(Base):
    __tablename__ = 'devices'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    secret_hash: Mapped[str] = mapped_column(String(64))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    last_seen: Mapped[float] = mapped_column(Float, default=0)


class Enrollment(Base):
    __tablename__ = 'enrollments'
    code_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    expires: Mapped[float] = mapped_column(Float)
    used: Mapped[bool] = mapped_column(Boolean, default=False)


class Ticket(Base):
    __tablename__ = 'tickets'
    ticket_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    device_id: Mapped[str] = mapped_column(ForeignKey('devices.id'))
    expires: Mapped[float] = mapped_column(Float)
    used: Mapped[bool] = mapped_column(Boolean, default=False)


class Claim(Base):
    __tablename__ = 'claims'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    secret_hash: Mapped[str] = mapped_column(String(64))
    device_id: Mapped[str] = mapped_column(ForeignKey('devices.id'))
    phone_name: Mapped[str] = mapped_column(String(80))
    match_code: Mapped[str] = mapped_column(String(8))
    expires: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(20), default='pending')


class Phone(Base):
    __tablename__ = 'phones'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires: Mapped[float] = mapped_column(Float)
    last_seen: Mapped[float] = mapped_column(Float)


class Grant(Base):
    __tablename__ = 'grants'
    __table_args__ = (Index('uq_grants_device_id', 'device_id', unique=True),)
    phone_id: Mapped[str] = mapped_column(ForeignKey('phones.id'), primary_key=True)
    device_id: Mapped[str] = mapped_column(ForeignKey('devices.id'), primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[float] = mapped_column(Float)


class Database:
    def __init__(self, url):
        options = {'pool_pre_ping': True}
        if url.startswith('sqlite'):
            options.update(connect_args={'check_same_thread': False})
            if ':memory:' in url:
                options['poolclass'] = StaticPool
        self.engine = create_engine(url, **options)
        self.session = sessionmaker(self.engine, expire_on_commit=False)

    def initialize(self):
        # create_all does not add indexes to tables that already exist. Upgrade
        # the original grants table explicitly, never silently choose/delete a
        # phone if an older deployment allowed several on the same device.
        with self.engine.begin() as connection:
            if inspect(connection).has_table('grants'):
                if connection.dialect.name == 'postgresql':
                    connection.execute(text('LOCK TABLE grants IN SHARE ROW EXCLUSIVE MODE'))
                duplicates = connection.execute(select(Grant.device_id).group_by(Grant.device_id)
                                                .having(func.count() > 1)).first()
                if duplicates:
                    raise RuntimeError('Single-phone migration refused: existing devices have multiple phone grants. '
                                       'Review and revoke the unwanted grants explicitly before restarting; no data was deleted.')
            Base.metadata.create_all(connection)
            for index in Grant.__table__.indexes:
                if index.name == 'uq_grants_device_id':
                    index.create(connection, checkfirst=True)
            installed = next((index for index in inspect(connection).get_indexes('grants')
                              if index['name'] == 'uq_grants_device_id'), None)
            if not installed or not installed['unique'] or installed['column_names'] != ['device_id']:
                raise RuntimeError('Single-phone migration refused: uq_grants_device_id is not the expected unique device index.')

    def issue_enrollment(self, minutes=10):
        value = token()
        with self.session.begin() as session:
            session.add(Enrollment(code_hash=digest(value), expires=time.time() + minutes * 60))
        return value
