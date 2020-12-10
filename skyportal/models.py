import yaml
import uuid
import re
import json
import warnings
from datetime import datetime, timezone, timedelta
import requests
import arrow
import abc

import astroplan
import numpy as np
import timezonefinder
from slugify import slugify

import sqlalchemy as sa
from sqlalchemy import cast, event
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects import postgresql as psql
from sqlalchemy.orm import relationship, aliased
from sqlalchemy.schema import UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.hybrid import hybrid_property, hybrid_method
from sqlalchemy.sql.expression import FromClause
from sqlalchemy_utils import URLType, EmailType
from sqlalchemy import func

from twilio.rest import Client as TwilioClient

from astropy import units as u
from astropy import time as ap_time
from astropy.utils.exceptions import AstropyWarning
from astropy import coordinates as ap_coord
from astropy.io import fits, ascii
import healpix_alchemy as ha

from .utils.cosmology import establish_cosmology
from baselayer.app.models import (  # noqa
    init_db,
    join_model,
    Base,
    DBSession,
    ACL,
    Role,
    User,
    Token,
    UserACL,
    UserRole,
)
from baselayer.app.custom_exceptions import AccessError
from baselayer.app.env import load_env
from baselayer.app.json_util import to_json

from . import schema
from .enum_types import (
    allowed_bandpasses,
    thumbnail_types,
    instrument_types,
    followup_priorities,
    api_classnames,
    listener_classnames,
)
from .email_utils import send_email

from skyportal import facility_apis

# In the AB system, a brightness of 23.9 mag corresponds to 1 microJy.
# All DB fluxes are stored in microJy (AB).
PHOT_ZP = 23.9
PHOT_SYS = 'ab'

utcnow = func.timezone('UTC', func.current_timestamp())

_, cfg = load_env()
cosmo = establish_cosmology(cfg)


def user_acls_temporary_table():
    """This method creates a temporary table that maps user_ids to their
    acl_ids (from roles and individual ACL grants).

    The temporary table lives for the duration of the current database
    transaction and is visible only to the transaction it was created
    within. The temporary table maintains a forward and reverse index
    for fast joins on accessible groups.

    This function can be called many times within a transaction. It will only
    create the table once per transaction and subsequent calls will always
    return a reference to the table created on the first call, with the same
    underlying data.

    Returns
    -------
    table: `sqlalchemy.Table`
        The forward- and reverse-indexed `merged_user_acls` temporary
        table for the current database transaction.
    """
    sql = """CREATE TEMP TABLE IF NOT EXISTS merged_user_acls ON COMMIT DROP AS
    (SELECT u.id AS user_id, ra.acl_id AS acl_id
         FROM users u
         JOIN user_roles ur ON u.id = ur.user_id
         JOIN role_acls ra ON ur.role_id = ra.role_id
         UNION
     SELECT ua.user_id, ua.acl_id FROM user_acls ua)"""
    DBSession().execute(sql)
    DBSession().execute(
        'CREATE INDEX IF NOT EXISTS merged_user_acls_forward_index '
        'ON merged_user_acls (user_id, acl_id)'
    )
    DBSession().execute(
        'CREATE INDEX IF NOT EXISTS merged_user_acls_reverse_index '
        'ON merged_user_acls (acl_id, user_id)'
    )

    t = sa.Table(
        'merged_user_acls',
        Base.metadata,
        sa.Column('user_id', sa.Integer, primary_key=True),
        sa.Column('acl_id', sa.Integer, primary_key=True),
        extend_existing=True,
    )
    return t


def user_accessible_groups_temporary_table():
    """This method creates a temporary table that maps user_ids to their
    accessible group_ids. For normal users, accessible groups are identical
    to what is in the group_users table. For users with the "System admin" ACL,
    all groups are accessible.

    The temporary table lives for the duration of the current database
    transaction and is visible only to the transaction it was created
    within. The temporary table maintains a forward and reverse index
    for fast joins on accessible groups.

    This function can be called many times within a transaction. It will only
    create the table once per transaction and subsequent calls will always
    return a reference to the table created on the first call, with the same
    underlying data.

    Returns
    -------
    table: `sqlalchemy.Table`
        The forward- and reverse-indexed `user_accessible_groups` temporary
        table for the current database transaction.
    """

    user_acls = user_acls_temporary_table()

    sql = f"""
CREATE TEMP TABLE IF NOT EXISTS user_accessible_groups ON COMMIT DROP
AS
  (SELECT user_id,
          group_id
   FROM group_users
   UNION SELECT sq.user_id,
                sq.group_id
   FROM
     (SELECT uacl.user_id AS user_id,
             g.id AS group_id
      FROM
        {user_acls.name} uacl
      JOIN groups g ON uacl.acl_id = 'System admin') sq);"""
    DBSession().execute(sql)
    DBSession().execute(
        'CREATE INDEX IF NOT EXISTS user_accessible_groups_forward_index '
        'ON user_accessible_groups (user_id, group_id)'
    )
    DBSession().execute(
        'CREATE INDEX IF NOT EXISTS user_accessible_groups_reverse_index '
        'ON user_accessible_groups (group_id, user_id)'
    )

    t = sa.Table(
        'user_accessible_groups',
        Base.metadata,
        sa.Column('user_id', sa.Integer, primary_key=True),
        sa.Column('group_id', sa.Integer, primary_key=True),
        extend_existing=True,
    )
    return t


def get_app_base_url():
    ports_to_ignore = {True: 443, False: 80}  # True/False <-> server.ssl=True/False
    return f"{'https' if cfg['server.ssl'] else 'http'}:" f"//{cfg['server.host']}" + (
        f":{cfg['server.port']}"
        if (
            cfg["server.port"] is not None
            and cfg["server.port"] != ports_to_ignore[cfg["server.ssl"]]
        )
        else ""
    )


def basic_user_display_info(user):
    return {
        field: getattr(user, field)
        for field in ('username', 'first_name', 'last_name', 'gravatar_url')
    }


def user_to_dict(self):
    return {
        field: getattr(self, field)
        for field in User.__table__.columns.keys()
        if field != "preferences"
    }


User.to_dict = user_to_dict


def _is_accessible(self, user_or_token, access_func):
    cls = type(self)
    return (
        DBSession().query(access_func(user_or_token)).filter(cls.id == self.id).scalar()
    )


def _bulk_retrieve(
    cls, user_or_token, opname, accessible_pair_func, required_attrs, options=[]
):
    for attr in required_attrs:
        if not hasattr(cls, attr):
            raise TypeError(
                f'{cls} does not have the attribute "{attr}", '
                f'and thus does not expose the interface that is needed '
                f'to check if {opname}.'
            )

    if isinstance(user_or_token, User):
        accessibility_target = user_or_token.id
    elif isinstance(user_or_token, Token):
        accessibility_target = user_or_token.created_by_id
    else:
        raise TypeError(
            f'Invalid argument passed to user_or_token, '
            f'expected User or Token, got '
            f'{user_or_token.__class__.__name__}'
        )

    pairs = accessible_pair_func(cls.__table__, User.__table__).alias()

    return (
        DBSession()
        .query(cls)
        .join(User, sa.literal(True))
        .join(pairs, sa.and_(User.id == pairs.c.user_id, cls.id == pairs.c.cls_id))
        .filter(User.id == accessibility_target)
        .options(options)
    )


def _is_accessible_sql(
    cls, user_or_token, opname, accessible_pair_func, required_attrs
):
    for attr in required_attrs:
        if not hasattr(cls, attr):
            raise TypeError(
                f'{cls} does not have the attribute "{attr}", '
                f'and thus does not expose the interface that is needed '
                f'to check if {opname}.'
            )

    if isinstance(user_or_token, FromClause):
        if hasattr(user_or_token.c, 'created_by_id'):
            accessibility_target = user_or_token.c.created_by_id
        else:
            accessibility_target = user_or_token.c.id
    elif isinstance(user_or_token, sa.Column):
        accessibility_target = user_or_token
    elif user_or_token is Token or isinstance(user_or_token, Token):
        accessibility_target = user_or_token.created_by_id
    else:
        accessibility_target = user_or_token.id

    correlation_cls_alias = sa.alias(cls)
    correlation_user_alias = sa.alias(User)
    accessible_pairs = accessible_pair_func(
        correlation_cls_alias, correlation_user_alias
    ).lateral()

    return (
        sa.select([accessible_pairs.c.cls_id.isnot(None)])
        .select_from(
            sa.join(
                correlation_cls_alias, correlation_user_alias, sa.literal(True)
            ).outerjoin(
                accessible_pairs,
                correlation_cls_alias.c.id == accessible_pairs.c.cls_id,
            )
        )
        .where(correlation_cls_alias.c.id == cls.id)
        .where(correlation_user_alias.c.id == accessibility_target)
        .label(opname)
        .is_(True)
    )


def _get_if_accessible_by(cls, cls_id, user_or_token, access_func_name, options=[]):
    instance = cls.query.options(options).get(cls_id)
    if instance is not None:
        access_func = getattr(instance, access_func_name)
        if not access_func(user_or_token):
            raise AccessError('Insufficient permissions.')
    return instance


def make_permission_control(name, opname):

    access_func_name = f'{opname}_by'
    pair_table_func_name = f'_{opname}_pair_table'
    get_classmethod_name = f'get_if_{opname}_by'
    required_attributes_func_name = f'_required_attributes_for_{opname}_check'
    bulk_func_name = f'retrieve_all_records_{opname}_by'

    @hybrid_method
    def is_accessible(self, user_or_token):
        cls = type(self)
        access_func = getattr(cls, access_func_name)
        return _is_accessible(self, user_or_token, access_func)

    @is_accessible.expression
    def is_accessible(cls, user_or_token):
        pair_table_func = getattr(cls, pair_table_func_name)
        required_attrs = getattr(cls, required_attributes_func_name)()
        return _is_accessible_sql(
            cls, user_or_token, opname, pair_table_func, required_attrs
        )

    @classmethod
    def get_classmethod(cls, cls_id, user_or_token, options=[]):
        return _get_if_accessible_by(
            cls, cls_id, user_or_token, access_func_name, options=options
        )

    @classmethod
    def bulk_retrieve(cls, user_or_token, options=[]):
        pair_table_func = getattr(cls, pair_table_func_name)
        required_attrs = getattr(cls, required_attributes_func_name)()
        return _bulk_retrieve(
            cls, user_or_token, opname, pair_table_func, required_attrs, options=options
        )

    @classmethod
    @abc.abstractmethod
    def _pair_table(cls, correlation_cls_alias, correlation_user_alias):
        return NotImplemented

    @classmethod
    @abc.abstractmethod
    def _required_attributes(cls):
        return NotImplemented

    class_dict = {
        get_classmethod_name: get_classmethod,
        pair_table_func_name: _pair_table,
        required_attributes_func_name: _required_attributes,
        access_func_name: is_accessible,
        bulk_func_name: bulk_retrieve,
    }
    return type(name, (), class_dict)


ReadProtected = make_permission_control('ReadProtected', 'readable')
WriteProtected = make_permission_control('WriteProtected', 'is_modifiable')


class ReadableByGroupMembers(ReadProtected):
    @classmethod
    def _required_attributes_for_readable_check(cls):
        return ('group',)

    @classmethod
    def _readable_pair_table(cls, correlation_cls_alias, correlation_user_alias):

        cls_alias = sa.alias(cls)
        user_accessible_groups = user_accessible_groups_temporary_table()

        readable_by_virtue_of_groups = (
            sa.select(
                [
                    cls_alias.c.id.label('cls_id'),
                    user_accessible_groups.c.user_id.label('user_id'),
                ]
            )
            .select_from(
                sa.join(
                    cls_alias,
                    user_accessible_groups,
                    cls_alias.c.group_id == user_accessible_groups.c.group_id,
                )
            )
            .where(cls_alias.c.id == correlation_cls_alias.c.id)
            .where(user_accessible_groups.c.user_id == correlation_user_alias.c.id)
        )

        return readable_by_virtue_of_groups.distinct()


class ReadableByGroupsMembers(ReadProtected):
    @classmethod
    def _required_attributes_for_readable_check(cls):
        return ('groups',)

    @classmethod
    def _readable_pair_table(cls, correlation_cls_alias, correlation_user_alias):

        cls_alias = sa.alias(cls)
        cls_groups_join_table = sa.inspect(cls).relationships['groups'].secondary
        user_accessible_groups = user_accessible_groups_temporary_table()

        readable_by_virtue_of_groups = (
            sa.select(
                [
                    cls_alias.c.id.label('cls_id'),
                    user_accessible_groups.c.user_id.label('user_id'),
                ]
            )
            .select_from(
                sa.join(
                    cls_alias,
                    cls_groups_join_table,
                    # automatically detects foreign key
                ).join(
                    user_accessible_groups,
                    cls_groups_join_table.c.group_id
                    == user_accessible_groups.c.group_id,
                )
            )
            .where(cls_alias.c.id == correlation_cls_alias.c.id)
            .where(user_accessible_groups.c.user_id == correlation_user_alias.c.id)
        )

        return readable_by_virtue_of_groups.distinct()


class ReadableByGroupsMembersIfObjIsReadable(ReadProtected):
    @classmethod
    def _required_attributes_for_readable_check(cls):
        return 'groups', 'obj'

    @classmethod
    def _readable_pair_table(cls, correlation_cls_alias, correlation_user_alias):

        cls_alias = sa.alias(cls)
        cls_groups_join_table = sa.inspect(cls).relationships['groups'].secondary
        user_accessible_groups = user_accessible_groups_temporary_table()
        obj_alias = aliased(Obj)

        readable_by_virtue_of_groups = (
            sa.select(
                [
                    cls_alias.c.id.label('cls_id'),
                    user_accessible_groups.c.user_id.label('user_id'),
                ]
            )
            .select_from(
                sa.join(
                    cls_alias,
                    cls_groups_join_table,
                    # automatically detects foreign key
                )
                .join(
                    user_accessible_groups,
                    cls_groups_join_table.c.group_id
                    == user_accessible_groups.c.group_id,
                )
                .join(
                    obj_alias,
                    obj_alias.is_readable_by(user_accessible_groups.c.user_id),
                )
            )
            .where(cls_alias.c.id == correlation_cls_alias.c.id)
            .where(user_accessible_groups.c.user_id == correlation_user_alias.c.id)
            .where(obj_alias.id == cls_alias.c.obj_id)
        )

        return readable_by_virtue_of_groups.distinct()


class ReadableByFilterGroupMembers(ReadProtected):
    @classmethod
    def _required_attributes_for_readable_check(cls):
        return ('filter',)

    @classmethod
    def _readable_pair_table(cls, correlation_cls_alias, correlation_user_alias):

        cls_alias = sa.alias(cls)
        user_accessible_groups = user_accessible_groups_temporary_table()

        readable_by_virtue_of_groups = (
            sa.select(
                [
                    cls_alias.c.id.label('cls_id'),
                    user_accessible_groups.c.user_id.label('user_id'),
                ]
            )
            .select_from(
                sa.join(
                    cls_alias,
                    Filter,
                    # automatically detects foreign key
                ).join(
                    user_accessible_groups,
                    Filter.group_id == user_accessible_groups.c.group_id,
                )
            )
            .where(cls_alias.c.id == correlation_cls_alias.c.id)
            .where(user_accessible_groups.c.user_id == correlation_user_alias.c.id)
        )

        return readable_by_virtue_of_groups.distinct()


class ReadableIfObjIsReadable(ReadProtected):
    @classmethod
    def _required_attributes_for_readable_check(cls):
        return ('obj',)

    @classmethod
    def _readable_pair_table(cls, correlation_cls_alias, correlation_user_alias):

        cls_alias = sa.alias(cls)
        user_alias = sa.alias(User)
        obj_alias = aliased(Obj)

        readable_by_virtue_of_obj = (
            sa.select(
                [cls_alias.c.id.label('cls_id'), user_alias.c.id.label('user_id')]
            )
            .select_from(
                sa.join(cls_alias, obj_alias, obj_alias.id == cls_alias.c.obj_id).join(
                    user_alias, obj_alias.is_readable_by(user_alias)
                )
            )
            .where(cls_alias.c.id == correlation_cls_alias.c.id)
            .where(user_alias.c.id == correlation_user_alias.c.id)
            .where(obj_alias.id == cls_alias.c.obj_id)
        )

        return readable_by_virtue_of_obj.distinct()


class ModifiableByOwner(WriteProtected):
    @classmethod
    def _required_attributes_for_is_modifiable_check(cls):
        return ('owner',)

    @classmethod
    def _is_modifiable_pair_table(cls, correlation_cls_alias, correlation_user_alias):
        cls_alias = sa.alias(cls)
        user_alias = sa.alias(User)
        user_acls = user_acls_temporary_table()

        modifiable_by_virtue_of_owner = (
            sa.select(
                [cls_alias.c.id.label('cls_id'), user_alias.c.id.label('user_id')]
            )
            .select_from(
                sa.join(cls_alias, user_alias, cls_alias.c.owner_id == user_alias.c.id,)
            )
            .where(cls_alias.c.id == correlation_cls_alias.c.id)
            .where(user_alias.c.id == correlation_user_alias.c.id)
        )

        modifiable_by_virtue_of_acl = (
            sa.select([cls_alias.c.id, user_acls.c.user_id])
            .select_from(
                sa.join(
                    correlation_cls_alias,
                    user_acls,
                    user_acls.c.acl_id == 'System admin',
                )
            )
            .where(cls_alias.c.id == correlation_cls_alias.c.id)
            .where(user_acls.c.user_id == correlation_user_alias.c.id)
        )

        return sa.union(modifiable_by_virtue_of_owner, modifiable_by_virtue_of_acl)


class NumpyArray(sa.types.TypeDecorator):
    """SQLAlchemy representation of a NumPy array."""

    impl = psql.ARRAY(sa.Float)

    def process_result_value(self, value, dialect):
        return np.array(value)


class Group(Base):
    """A user group. `Group`s controls `User` access to `Filter`s and serve as
    targets for data sharing requests. `Photometry` and `Spectra` shared with
    a `Group` will be visible to all its members. `Group`s maintain specific
    `Stream` permissions. In order for a `User` to join a `Group`, the `User`
    must have access to all of the `Group`'s data `Stream`s.
    """

    name = sa.Column(
        sa.String, unique=True, nullable=False, index=True, doc='Name of the group.'
    )
    nickname = sa.Column(
        sa.String, unique=True, nullable=True, index=True, doc='Short group nickname.'
    )

    streams = relationship(
        'Stream',
        secondary='group_streams',
        back_populates='groups',
        passive_deletes=True,
        doc='Stream access required for a User to become a member of the Group.',
    )
    filters = relationship(
        "Filter",
        back_populates="group",
        passive_deletes=True,
        doc='All filters (not just active) associated with a group.',
    )

    users = relationship(
        'User',
        secondary='group_users',
        back_populates='groups',
        passive_deletes=True,
        doc='The members of this group.',
    )

    group_users = relationship(
        'GroupUser',
        back_populates='group',
        cascade='save-update, merge, refresh-expire, expunge',
        passive_deletes=True,
        doc='Elements of a join table mapping Users to Groups.',
    )

    observing_runs = relationship(
        'ObservingRun',
        back_populates='group',
        doc='The observing runs associated with this group.',
    )
    photometry = relationship(
        "Photometry",
        secondary="group_photometry",
        back_populates="groups",
        cascade="save-update, merge, refresh-expire, expunge",
        passive_deletes=True,
        doc='The photometry visible to this group.',
    )

    spectra = relationship(
        "Spectrum",
        secondary="group_spectra",
        back_populates="groups",
        cascade="save-update, merge, refresh-expire, expunge",
        passive_deletes=True,
        doc='The spectra visible to this group.',
    )
    single_user_group = sa.Column(
        sa.Boolean,
        default=False,
        index=True,
        doc='Flag indicating whether this group '
        'is a singleton group for one user only.',
    )
    allocations = relationship(
        'Allocation',
        back_populates="group",
        cascade="save-update, merge, refresh-expire, expunge",
        passive_deletes=True,
        doc="Allocations made to this group.",
    )


GroupUser = join_model('group_users', Group, User)
GroupUser.__doc__ = "Join table mapping `Group`s to `User`s."

GroupUser.admin = sa.Column(
    sa.Boolean,
    nullable=False,
    default=False,
    doc="Boolean flag indicating whether the User is an admin of the group.",
)


class Stream(Base):
    """A data stream producing alerts that can be programmatically filtered
    using a Filter. """

    name = sa.Column(sa.String, unique=True, nullable=False, doc="Stream name.")
    altdata = sa.Column(
        JSONB,
        nullable=True,
        doc="Misc. metadata stored in JSON format, e.g. "
        "`{'collection': 'ZTF_alerts', selector: [1, 2]}`",
    )

    groups = relationship(
        'Group',
        secondary='group_streams',
        back_populates='streams',
        passive_deletes=True,
        doc="The Groups with access to this Stream.",
    )
    users = relationship(
        'User',
        secondary='stream_users',
        back_populates='streams',
        passive_deletes=True,
        doc="The users with access to this stream.",
    )
    filters = relationship(
        'Filter',
        back_populates='stream',
        passive_deletes=True,
        doc="The filters with access to this stream.",
    )


GroupStream = join_model('group_streams', Group, Stream)
GroupStream.__doc__ = "Join table mapping Groups to Streams."


StreamUser = join_model('stream_users', Stream, User)
StreamUser.__doc__ = "Join table mapping Streams to Users."


User.groups = relationship(
    'Group',
    secondary='group_users',
    back_populates='users',
    passive_deletes=True,
    doc="The Groups this User is a member of.",
)


User.streams = relationship(
    'Stream',
    secondary='stream_users',
    back_populates='users',
    passive_deletes=True,
    doc="The Streams this User has access to.",
)


User.single_user_group = property(
    lambda self: DBSession()
    .query(Group)
    .join(GroupUser)
    .filter(Group.single_user_group.is_(True), GroupUser.user_id == self.id)
    .first()
)


@property
def user_or_token_accessible_groups(self):
    """Return the list of Groups a User or Token has access to. For non-admin
    Users or Token owners, this corresponds to the Groups they are a member of.
    For System Admins, this corresponds to all Groups."""
    if "System admin" in self.permissions:
        return Group.query.all()
    return self.groups


User.accessible_groups = user_or_token_accessible_groups
Token.accessible_groups = user_or_token_accessible_groups


@property
def user_or_token_accessible_streams(self):
    """Return the list of Streams a User or Token has access to."""
    if "System admin" in self.permissions:
        return Stream.query.all()
    if isinstance(self, Token):
        return self.created_by.streams
    return self.streams


User.accessible_streams = user_or_token_accessible_streams
Token.accessible_streams = user_or_token_accessible_streams


@property
def token_groups(self):
    """The groups the Token owner is a member of."""
    return self.created_by.groups


Token.groups = token_groups


class Obj(
    ReadProtected, Base, ha.Point,
):
    """A record of an astronomical Object and its metadata, such as position,
    positional uncertainties, name, and redshift."""

    id = sa.Column(sa.String, primary_key=True, doc="Name of the object.")
    # TODO should this column type be decimal? fixed-precison numeric

    ra_dis = sa.Column(sa.Float, doc="J2000 Right Ascension at discovery time [deg].")
    dec_dis = sa.Column(sa.Float, doc="J2000 Declination at discovery time [deg].")

    ra_err = sa.Column(
        sa.Float,
        nullable=True,
        doc="Error on J2000 Right Ascension at discovery time [deg].",
    )
    dec_err = sa.Column(
        sa.Float,
        nullable=True,
        doc="Error on J2000 Declination at discovery time [deg].",
    )

    offset = sa.Column(
        sa.Float, default=0.0, doc="Offset from nearest static object [arcsec]."
    )
    redshift = sa.Column(sa.Float, nullable=True, doc="Redshift.")
    redshift_history = sa.Column(
        JSONB, nullable=True, doc="Record of who set which redshift values and when.",
    )

    # Contains all external metadata, e.g. simbad, pan-starrs, tns, gaia
    altdata = sa.Column(
        JSONB,
        nullable=True,
        doc="Misc. alternative metadata stored in JSON format, e.g. "
        "`{'gaia': {'info': {'Teff': 5780}}}`",
    )

    dist_nearest_source = sa.Column(
        sa.Float, nullable=True, doc="Distance to the nearest Obj [arcsec]."
    )
    mag_nearest_source = sa.Column(
        sa.Float, nullable=True, doc="Magnitude of the nearest Obj [AB]."
    )
    e_mag_nearest_source = sa.Column(
        sa.Float, nullable=True, doc="Error on magnitude of the nearest Obj [mag]."
    )

    transient = sa.Column(
        sa.Boolean,
        default=False,
        doc="Boolean indicating whether the object is an astrophysical transient.",
    )
    varstar = sa.Column(
        sa.Boolean,
        default=False,
        doc="Boolean indicating whether the object is a variable star.",
    )
    is_roid = sa.Column(
        sa.Boolean,
        default=False,
        doc="Boolean indicating whether the object is a moving object.",
    )

    score = sa.Column(sa.Float, nullable=True, doc="Machine learning score.")

    origin = sa.Column(sa.String, nullable=True, doc="Origin of the object.")

    internal_key = sa.Column(
        sa.String,
        nullable=False,
        default=lambda: str(uuid.uuid4()),
        doc="Internal key used for secure websocket messaging.",
    )

    candidates = relationship(
        'Candidate',
        back_populates='obj',
        cascade='save-update, merge, refresh-expire, expunge',
        passive_deletes=True,
        order_by="Candidate.passed_at",
        doc="Candidates associated with the object.",
    )
    comments = relationship(
        'Comment',
        back_populates='obj',
        cascade='save-update, merge, refresh-expire, expunge',
        passive_deletes=True,
        order_by="Comment.created_at",
        doc="Comments posted about the object.",
    )
    annotations = relationship(
        'Annotation',
        back_populates='obj',
        cascade='save-update, merge, refresh-expire, expunge',
        passive_deletes=True,
        order_by="Annotation.created_at",
        doc="Auto-annotations posted about the object.",
    )

    classifications = relationship(
        'Classification',
        back_populates='obj',
        cascade='save-update, merge, refresh-expire, expunge',
        passive_deletes=True,
        order_by="Classification.created_at",
        doc="Classifications of the object.",
    )

    photometry = relationship(
        'Photometry',
        back_populates='obj',
        cascade='save-update, merge, refresh-expire, expunge',
        single_parent=True,
        passive_deletes=True,
        order_by="Photometry.mjd",
        doc="Photometry of the object.",
    )

    detect_photometry_count = sa.Column(
        sa.Integer,
        nullable=True,
        doc="How many times the object was detected above :math:`S/N = 5`.",
    )

    spectra = relationship(
        'Spectrum',
        back_populates='obj',
        cascade='save-update, merge, refresh-expire, expunge',
        single_parent=True,
        passive_deletes=True,
        order_by="Spectrum.observed_at",
        doc="Spectra of the object.",
    )
    thumbnails = relationship(
        'Thumbnail',
        back_populates='obj',
        cascade='save-update, merge, refresh-expire, expunge',
        passive_deletes=True,
        doc="Thumbnails of the object.",
    )

    followup_requests = relationship(
        'FollowupRequest',
        back_populates='obj',
        doc="Robotic follow-up requests of the object.",
    )
    assignments = relationship(
        'ClassicalAssignment',
        back_populates='obj',
        doc="Assignments of the object to classical observing runs.",
    )

    obj_notifications = relationship(
        "SourceNotification",
        back_populates="source",
        doc="Notifications regarding the object sent out by users",
    )

    @hybrid_property
    def last_detected(self):
        """UTC ISO date at which the object was last detected above a S/N of 5."""
        detections = [phot.iso for phot in self.photometry if phot.snr and phot.snr > 5]
        return max(detections) if detections else None

    @last_detected.expression
    def last_detected(cls):
        """UTC ISO date at which the object was last detected above a S/N of 5."""
        return (
            sa.select([sa.func.max(Photometry.iso)])
            .where(Photometry.obj_id == cls.id)
            .where(Photometry.snr > 5.0)
            .group_by(Photometry.obj_id)
            .label('last_detected')
        )

    def add_linked_thumbnails(self):
        """Determine the URLs of the SDSS and DESI DR8 thumbnails of the object,
        insert them into the Thumbnails table, and link them to the object."""
        sdss_thumb = Thumbnail(obj=self, public_url=self.sdss_url, type='sdss')
        dr8_thumb = Thumbnail(obj=self, public_url=self.desi_dr8_url, type='dr8')
        DBSession().add_all([sdss_thumb, dr8_thumb])
        DBSession().commit()

    def add_ps1_thumbnail(self):
        ps1_thumb = Thumbnail(obj=self, public_url=self.panstarrs_url, type="ps1")
        DBSession().add(ps1_thumb)
        DBSession().commit()

    @property
    def sdss_url(self):
        """Construct URL for public Sloan Digital Sky Survey (SDSS) cutout."""
        return (
            f"https://skyserver.sdss.org/dr12/SkyserverWS/ImgCutout/getjpeg"
            f"?ra={self.ra}&dec={self.dec}&scale=0.3&width=200&height=200"
            f"&opt=G&query=&Grid=on"
        )

    @property
    def desi_dr8_url(self):
        """Construct URL for public DESI DR8 cutout."""
        return (
            f"https://www.legacysurvey.org/viewer/jpeg-cutout?ra={self.ra}"
            f"&dec={self.dec}&size=200&layer=dr8&pixscale=0.262&bands=grz"
        )

    @property
    def panstarrs_url(self):
        """Construct URL for public PanSTARRS-1 (PS1) cutout.

        The cutout service doesn't allow directly querying for an image; the
        best we can do is request a page that contains a link to the image we
        want (in this case a combination of the g/r/i filters).
        """
        ps_query_url = (
            f"https://ps1images.stsci.edu/cgi-bin/ps1cutouts"
            f"?pos={self.ra}+{self.dec}&filter=color&filter=g"
            f"&filter=r&filter=i&filetypes=stack&size=250"
        )
        response = requests.get(ps_query_url)
        match = re.search('src="//ps1images.stsci.edu.*?"', response.content.decode())
        return match.group().replace('src="', 'http:').replace('"', '')

    @property
    def target(self):
        """Representation of the RA and Dec of this Obj as an
        astroplan.FixedTarget."""
        coord = ap_coord.SkyCoord(self.ra, self.dec, unit='deg')
        return astroplan.FixedTarget(name=self.id, coord=coord)

    @property
    def gal_lat_deg(self):
        """Get the galactic latitute of this object"""
        coord = ap_coord.SkyCoord(self.ra, self.dec, unit="deg")
        return coord.galactic.b.deg

    @property
    def gal_lon_deg(self):
        """Get the galactic longitude of this object"""
        coord = ap_coord.SkyCoord(self.ra, self.dec, unit="deg")
        return coord.galactic.l.deg

    @property
    def luminosity_distance(self):
        """
        The luminosity distance in Mpc, using either DM or distance data
        in the altdata fields or using the cosmology/redshift. Specifically
        the user can add `dm` (mag), `parallax` (arcsec), `dist_kpc`,
        `dist_Mpc`, `dist_pc` or `dist_cm` to `altdata` and
        those will be picked up (in that order) as the distance
        rather than the redshift.

        Return None if the redshift puts the source not within the Hubble flow
        """

        # there may be a non-redshift based measurement of distance
        # for nearby sources
        if self.altdata:
            if self.altdata.get("dm") is not None:
                # see eq (24) of https://ned.ipac.caltech.edu/level5/Hogg/Hogg7.html
                return (
                    (10 ** (float(self.altdata.get("dm")) / 5.0)) * 1e-5 * u.Mpc
                ).value
            if self.altdata.get("parallax") is not None:
                if float(self.altdata.get("parallax")) > 0:
                    # assume parallax in arcsec
                    return (1e-6 * u.Mpc / float(self.altdata.get("parallax"))).value

            if self.altdata.get("dist_kpc") is not None:
                return (float(self.altdata.get("dist_kpc")) * 1e-3 * u.Mpc).value
            if self.altdata.get("dist_Mpc") is not None:
                return (float(self.altdata.get("dist_Mpc")) * u.Mpc).value
            if self.altdata.get("dist_pc") is not None:
                return (float(self.altdata.get("dist_pc")) * 1e-6 * u.Mpc).value
            if self.altdata.get("dist_cm") is not None:
                return (float(self.altdata.get("dist_cm")) * u.Mpc / 3.085e18).value

        if self.redshift:
            if self.redshift * 2.99e5 * u.km / u.s < 350 * u.km / u.s:
                # stubbornly refuse to give a distance if the source
                # is not in the Hubble flow
                # cf. https://www.aanda.org/articles/aa/full/2003/05/aa3077/aa3077.html
                # within ~5 Mpc (cz ~ 350 km/s) a given galaxy velocty
                # can be between between ~0-500 km/s
                return None
            return (cosmo.luminosity_distance(self.redshift)).to(u.Mpc).value
        return None

    @property
    def dm(self):
        """Distance modulus to the object"""
        dl = self.luminosity_distance
        if dl:
            return 5.0 * np.log10((dl * u.Mpc) / (10 * u.pc)).value
        return None

    @property
    def angular_diameter_distance(self):
        dl = self.luminosity_distance
        if dl:
            if self.redshift and self.redshift * 2.99e5 * u.km / u.s > 350 * u.km / u.s:
                # see eq (20) of https://ned.ipac.caltech.edu/level5/Hogg/Hogg7.html
                return dl / (1 + self.redshift) ** 2
            return dl
        return None

    def airmass(self, telescope, time, below_horizon=np.inf):
        """Return the airmass of the object at a given time. Uses the Pickering
        (2002) interpolation of the Rayleigh (molecular atmosphere) airmass.

        The Pickering interpolation tends toward 38.7494 as the altitude
        approaches zero.

        Parameters
        ----------
        telescope : `skyportal.models.Telescope`
            The telescope to use for the airmass calculation
        time : `astropy.time.Time` or list of astropy.time.Time`
            The time or times at which to calculate the airmass
        below_horizon : scalar, Numeric
            Airmass value to assign when an object is below the horizon.
            An object is "below the horizon" when its altitude is less than
            zero degrees.

        Returns
        -------
        airmass : ndarray
           The airmass of the Obj at the requested times
        """

        output_shape = np.shape(time)
        time = np.atleast_1d(time)
        altitude = self.altitude(telescope, time).to('degree').value
        above = altitude > 0

        # use Pickering (2002) interpolation to calculate the airmass
        # The Pickering interpolation tends toward 38.7494 as the altitude
        # approaches zero.
        sinarg = np.zeros_like(altitude)
        airmass = np.ones_like(altitude) * np.inf
        sinarg[above] = altitude[above] + 244 / (165 + 47 * altitude[above] ** 1.1)
        airmass[above] = 1.0 / np.sin(np.deg2rad(sinarg[above]))

        # set objects below the horizon to an airmass of infinity
        airmass[~above] = below_horizon
        airmass = airmass.reshape(output_shape)

        return airmass

    def altitude(self, telescope, time):
        """Return the altitude of the object at a given time.

        Parameters
        ----------
        telescope : `skyportal.models.Telescope`
            The telescope to use for the altitude calculation

        time : `astropy.time.Time`
            The time or times at which to calculate the altitude

        Returns
        -------
        alt : `astropy.coordinates.AltAz`
           The altitude of the Obj at the requested times
        """

        return telescope.observer.altaz(time, self.target).alt

    @classmethod
    def _required_attributes_for_readable_check(cls):
        return ()

    @classmethod
    def _readable_pair_table(cls, correlation_cls_alias, correlation_user_alias):
        cand_x_filt = sa.join(Candidate, Filter)
        phot_x_groupphot = sa.join(Photometry, GroupPhotometry)
        unified_group_users = user_accessible_groups_temporary_table()

        source_subq = (
            sa.select([Source.obj_id, unified_group_users.c.user_id])
            .select_from(
                sa.join(
                    Source,
                    unified_group_users,
                    Source.group_id == unified_group_users.c.group_id,
                )
            )
            .where(Source.obj_id == correlation_cls_alias.c.id)
            .where(unified_group_users.c.user_id == correlation_user_alias.c.id)
        )

        cand_subq = (
            sa.select([Candidate.obj_id, unified_group_users.c.user_id])
            .select_from(
                sa.join(
                    cand_x_filt,
                    unified_group_users,
                    Filter.group_id == unified_group_users.c.group_id,
                )
            )
            .where(Candidate.obj_id == correlation_cls_alias.c.id)
            .where(unified_group_users.c.user_id == correlation_user_alias.c.id)
        )

        phot_subq = (
            sa.select(
                [
                    Photometry.obj_id.label('cls_id'),
                    unified_group_users.c.user_id.label('user_id'),
                ]
            )
            .select_from(
                sa.join(
                    phot_x_groupphot,
                    unified_group_users,
                    GroupPhotometry.group_id == unified_group_users.c.group_id,
                )
            )
            .where(Photometry.obj_id == correlation_cls_alias.c.id)
            .where(unified_group_users.c.user_id == correlation_user_alias.c.id)
        )

        return sa.union(phot_subq, source_subq, cand_subq)


class Filter(ReadableByGroupMembers, Base):
    """An alert filter that operates on a Stream. A Filter is associated
    with exactly one Group, and a Group may have multiple operational Filters.
    """

    name = sa.Column(sa.String, nullable=False, unique=False, doc="Filter name.")
    stream_id = sa.Column(
        sa.ForeignKey("streams.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        doc="ID of the Filter's Stream.",
    )
    stream = relationship(
        "Stream",
        foreign_keys=[stream_id],
        back_populates="filters",
        doc="The Filter's Stream.",
    )
    group_id = sa.Column(
        sa.ForeignKey("groups.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        doc="ID of the Filter's Group.",
    )
    group = relationship(
        "Group",
        foreign_keys=[group_id],
        back_populates="filters",
        doc="The Filter's Group.",
    )
    candidates = relationship(
        'Candidate',
        back_populates='filter',
        cascade='save-update, merge, refresh-expire, expunge',
        order_by="Candidate.passed_at",
        doc="Candidates that have passed the filter.",
    )


class Candidate(ReadableByFilterGroupMembers, Base):
    "An Obj that passed a Filter, becoming scannable on the Filter's scanning page."
    obj_id = sa.Column(
        sa.ForeignKey("objs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        doc="ID of the Obj",
    )
    obj = relationship(
        "Obj",
        foreign_keys=[obj_id],
        back_populates="candidates",
        doc="The Obj that passed a filter",
    )
    filter_id = sa.Column(
        sa.ForeignKey("filters.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        doc="ID of the filter the candidate passed",
    )
    filter = relationship(
        "Filter",
        foreign_keys=[filter_id],
        back_populates="candidates",
        doc="The filter that the Candidate passed",
    )
    passed_at = sa.Column(
        sa.DateTime,
        nullable=False,
        index=True,
        doc="ISO UTC time when the Candidate passed the Filter.",
    )
    passing_alert_id = sa.Column(
        sa.BigInteger,
        index=True,
        doc="ID of the latest Stream alert that passed the Filter.",
    )
    uploader_id = sa.Column(
        sa.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        doc="ID of the user that posted the candidate",
    )


Candidate.__table_args__ = (
    sa.Index(
        "candidates_main_index",
        Candidate.obj_id,
        Candidate.filter_id,
        Candidate.passed_at,
        unique=True,
    ),
)


Source = join_model("sources", Group, Obj, mixins=(ReadableByGroupMembers,))

Source.__doc__ = (
    "An Obj that has been saved to a Group. Once an Obj is saved as a Source, "
    "the Obj is shielded in perpetuity from automatic database removal. "
    "If a Source is 'unsaved', its 'active' flag is set to False, but "
    "it is not purged."
)

Source.saved_by_id = sa.Column(
    sa.ForeignKey("users.id"),
    nullable=True,
    unique=False,
    index=True,
    doc="ID of the User that saved the Obj to its Group.",
)
Source.saved_by = relationship(
    "User",
    foreign_keys=[Source.saved_by_id],
    backref="saved_sources",
    doc="User that saved the Obj to its Group.",
)
Source.saved_at = sa.Column(
    sa.DateTime,
    nullable=False,
    default=utcnow,
    index=True,
    doc="ISO UTC time when the Obj was saved to its Group.",
)
Source.active = sa.Column(
    sa.Boolean,
    server_default="true",
    doc="Whether the Obj is still 'active' as a Source in its Group. "
    "If this flag is set to False, the Source will not appear in the Group's "
    "sample.",
)
Source.requested = sa.Column(
    sa.Boolean,
    server_default="false",
    doc="True if the source has been shared with another Group, but not saved "
    "by the recipient Group.",
)

Source.unsaved_by_id = sa.Column(
    sa.ForeignKey("users.id"),
    nullable=True,
    unique=False,
    index=True,
    doc="ID of the User who unsaved the Source.",
)
Source.unsaved_by = relationship(
    "User", foreign_keys=[Source.unsaved_by_id], doc="User who unsaved the Source."
)
Source.unsaved_at = sa.Column(
    sa.DateTime, nullable=True, doc="ISO UTC time when the Obj was unsaved from Group.",
)

Obj.sources = relationship(
    Source, back_populates='obj', doc="Instances in which a group saved this Obj."
)
Obj.candidates = relationship(
    Candidate,
    back_populates='obj',
    doc="Instances in which this Obj passed a group's filter.",
)


User.sources = relationship(
    'Obj',
    backref='users',
    secondary='join(Group, sources).join(group_users)',
    primaryjoin='group_users.c.user_id == users.c.id',
    passive_deletes=True,
    doc='The Sources accessible to this User.',
)

isadmin = property(lambda self: "System admin" in self.permissions)
User.is_system_admin = isadmin
Token.is_system_admin = isadmin


class SourceView(Base):
    """Record of an instance in which a Source was viewed via the frontend or
    retrieved via the API (for use in the "Top Sources" widget).
    """

    obj_id = sa.Column(
        sa.ForeignKey('objs.id', ondelete='CASCADE'),
        nullable=False,
        unique=False,
        index=True,
        doc="Object ID for which the view was registered.",
    )
    username_or_token_id = sa.Column(
        sa.String,
        nullable=False,
        unique=False,
        doc="Username or token ID of the viewer.",
    )
    is_token = sa.Column(
        sa.Boolean,
        nullable=False,
        default=False,
        doc="Whether the viewer was a User or a Token.",
    )
    created_at = sa.Column(
        sa.DateTime,
        nullable=False,
        default=datetime.utcnow,
        index=True,
        doc="UTC timestamp of the view.",
    )


class Telescope(Base):
    """A ground or space-based observational facility that can host Instruments."""

    name = sa.Column(
        sa.String,
        unique=True,
        nullable=False,
        doc="Unabbreviated facility name (e.g., Palomar 200-inch Hale Telescope).",
    )
    nickname = sa.Column(
        sa.String, nullable=False, doc="Abbreviated facility name (e.g., P200)."
    )
    lat = sa.Column(sa.Float, nullable=True, doc='Latitude in deg.')
    lon = sa.Column(sa.Float, nullable=True, doc='Longitude in deg.')
    elevation = sa.Column(sa.Float, nullable=True, doc='Elevation in meters.')
    diameter = sa.Column(sa.Float, nullable=False, doc='Diameter in meters.')
    skycam_link = sa.Column(
        URLType, nullable=True, doc="Link to the telescope's sky camera."
    )
    robotic = sa.Column(
        sa.Boolean, default=False, nullable=False, doc="Is this telescope robotic?"
    )

    fixed_location = sa.Column(
        sa.Boolean,
        nullable=False,
        server_default='true',
        doc="Does this telescope have a fixed location (lon, lat, elev)?",
    )

    weather = sa.Column(JSONB, nullable=True, doc='Latest weather information')
    weather_retrieved_at = sa.Column(
        sa.DateTime, nullable=True, doc="When was the weather last retrieved?"
    )
    weather_link = sa.Column(
        URLType, nullable=True, doc="Link to the preferred weather site."
    )

    instruments = relationship(
        'Instrument',
        back_populates='telescope',
        cascade='save-update, merge, refresh-expire, expunge',
        passive_deletes=True,
        doc="The Instruments on this telescope.",
    )

    @property
    def observer(self):
        """Return an `astroplan.Observer` representing an observer at this
        facility, accounting for the latitude, longitude, elevation, and
        local time zone of the observatory (if ground based)."""
        try:
            return self._observer
        except AttributeError:
            tf = timezonefinder.TimezoneFinder(in_memory=True)
            local_tz = tf.closest_timezone_at(
                lng=self.lon, lat=self.lat, delta_degree=5
            )
            self._observer = astroplan.Observer(
                longitude=self.lon * u.deg,
                latitude=self.lat * u.deg,
                elevation=self.elevation * u.m,
                timezone=local_tz,
            )
        return self._observer

    def next_sunset(self, time=None):
        """The astropy timestamp of the next sunset after `time` at this site.
        If time=None, uses the current time."""
        if time is None:
            time = ap_time.Time.now()
        observer = self.observer
        return observer.sun_set_time(time, which='next')

    def next_sunrise(self, time=None):
        """The astropy timestamp of the next sunrise after `time` at this site.
        If time=None, uses the current time."""
        if time is None:
            time = ap_time.Time.now()
        observer = self.observer
        return observer.sun_rise_time(time, which='next')

    def next_twilight_evening_nautical(self, time=None):
        """The astropy timestamp of the next evening nautical (-12 degree)
        twilight at this site. If time=None, uses the current time."""
        if time is None:
            time = ap_time.Time.now()
        observer = self.observer
        return observer.twilight_evening_nautical(time, which='next')

    def next_twilight_morning_nautical(self, time=None):
        """The astropy timestamp of the next morning nautical (-12 degree)
        twilight at this site. If time=None, uses the current time."""
        if time is None:
            time = ap_time.Time.now()
        observer = self.observer
        return observer.twilight_morning_nautical(time, which='next')

    def next_twilight_evening_astronomical(self, time=None):
        """The astropy timestamp of the next evening astronomical (-18 degree)
        twilight at this site. If time=None, uses the current time."""
        if time is None:
            time = ap_time.Time.now()
        observer = self.observer
        return observer.twilight_evening_astronomical(time, which='next')

    def next_twilight_morning_astronomical(self, time=None):
        """The astropy timestamp of the next morning astronomical (-18 degree)
        twilight at this site. If time=None, uses the current time."""
        if time is None:
            time = ap_time.Time.now()
        observer = self.observer
        return observer.twilight_morning_astronomical(time, which='next')

    def ephemeris(self, time):

        sunrise = self.next_sunrise(time=time)
        sunset = self.next_sunset(time=time)

        if sunset > sunrise:
            sunset = self.observer.sun_set_time(time, which='previous')
            time = sunset - ap_time.TimeDelta(30, format='sec')

        twilight_morning_astronomical = self.next_twilight_morning_astronomical(
            time=time
        )
        twilight_evening_astronomical = self.next_twilight_evening_astronomical(
            time=time
        )

        twilight_morning_nautical = self.next_twilight_morning_nautical(time=time)
        twilight_evening_nautical = self.next_twilight_evening_nautical(time=time)

        return {
            'sunset_utc': sunset.isot,
            'sunrise_utc': sunrise.isot,
            'twilight_morning_astronomical_utc': twilight_morning_astronomical.isot,
            'twilight_evening_astronomical_utc': twilight_evening_astronomical.isot,
            'twilight_morning_nautical_utc': twilight_morning_nautical.isot,
            'twilight_evening_nautical_utc': twilight_evening_nautical.isot,
            'utc_offset_hours': self.observer.timezone.utcoffset(time.datetime)
            / timedelta(hours=1),
            'sunset_unix_ms': sunset.unix * 1000,
            'sunrise_unix_ms': sunrise.unix * 1000,
            'twilight_morning_astronomical_unix_ms': twilight_morning_astronomical.unix
            * 1000,
            'twilight_evening_astronomical_unix_ms': twilight_evening_astronomical.unix
            * 1000,
            'twilight_morning_nautical_unix_ms': twilight_morning_nautical.unix * 1000,
            'twilight_evening_nautical_unix_ms': twilight_evening_nautical.unix * 1000,
        }


class ArrayOfEnum(ARRAY):
    def bind_expression(self, bindvalue):
        return cast(bindvalue, self)

    def result_processor(self, dialect, coltype):
        super_rp = super(ArrayOfEnum, self).result_processor(dialect, coltype)

        def handle_raw_string(value):
            if value is None or value == '{}':  # 2nd case, empty array
                return []
            inner = re.match(r"^{(.*)}$", value).group(1)
            return inner.split(",")

        def process(value):
            return super_rp(handle_raw_string(value))

        return process


class Instrument(Base):
    """An instrument attached to a telescope."""

    name = sa.Column(sa.String, unique=True, nullable=False, doc="Instrument name.")
    type = sa.Column(
        instrument_types,
        nullable=False,
        doc="Instrument type, one of Imager, Spectrograph, or Imaging Spectrograph.",
    )

    band = sa.Column(
        sa.String,
        doc="The spectral band covered by the instrument " "(e.g., Optical, IR).",
    )
    telescope_id = sa.Column(
        sa.ForeignKey('telescopes.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
        doc="The ID of the Telescope that hosts the Instrument.",
    )
    telescope = relationship(
        'Telescope',
        back_populates='instruments',
        doc="The Telescope that hosts the Instrument.",
    )

    photometry = relationship(
        'Photometry',
        back_populates='instrument',
        doc="The Photometry produced by this instrument.",
    )
    spectra = relationship(
        'Spectrum',
        back_populates='instrument',
        doc="The Spectra produced by this instrument.",
    )

    # can be [] if an instrument is spec only
    filters = sa.Column(
        ArrayOfEnum(allowed_bandpasses),
        nullable=False,
        default=[],
        doc='List of filters on the instrument (if any).',
    )

    allocations = relationship(
        'Allocation',
        back_populates="instrument",
        cascade="save-update, merge, refresh-expire, expunge",
        passive_deletes=True,
    )
    observing_runs = relationship(
        'ObservingRun',
        back_populates='instrument',
        doc="List of ObservingRuns on the Instrument.",
    )

    api_classname = sa.Column(
        api_classnames, nullable=True, doc="Name of the instrument's API class."
    )

    listener_classname = sa.Column(
        listener_classnames,
        nullable=True,
        doc="Name of the instrument's listener class.",
    )

    @property
    def does_spectroscopy(self):
        """Return a boolean indicating whether the instrument is capable of
        performing spectroscopy."""
        return 'spec' in self.type

    @property
    def does_imaging(self):
        """Return a boolean indicating whether the instrument is capable of
        performing imaging."""
        return 'imag' in self.type

    @property
    def api_class(self):
        return getattr(facility_apis, self.api_classname)

    @property
    def listener_class(self):
        return getattr(facility_apis, self.listener_classname)


class Allocation(ReadableByGroupMembers, Base):
    """An allocation of observing time on a robotic instrument."""

    pi = sa.Column(sa.String, doc="The PI of the allocation's proposal.")
    proposal_id = sa.Column(
        sa.String, doc="The ID of the proposal associated with this allocation."
    )
    start_date = sa.Column(sa.DateTime, doc='The UTC start date of the allocation.')
    end_date = sa.Column(sa.DateTime, doc='The UTC end date of the allocation.')
    hours_allocated = sa.Column(
        sa.Float, nullable=False, doc='The number of hours allocated.'
    )
    requests = relationship(
        'FollowupRequest',
        back_populates='allocation',
        doc='The requests made against this allocation.',
    )

    group_id = sa.Column(
        sa.ForeignKey('groups.id', ondelete='CASCADE'),
        index=True,
        doc='The ID of the Group the allocation is associated with.',
        nullable=False,
    )
    group = relationship(
        'Group',
        back_populates='allocations',
        doc='The Group the allocation is associated with.',
    )

    instrument_id = sa.Column(
        sa.ForeignKey('instruments.id', ondelete='CASCADE'),
        index=True,
        doc="The ID of the Instrument the allocation is associated with.",
        nullable=False,
    )
    instrument = relationship(
        'Instrument',
        back_populates='allocations',
        doc="The Instrument the allocation is associated with.",
    )


class Taxonomy(ReadableByGroupsMembers, Base):
    """An ontology within which Objs can be classified."""

    __tablename__ = 'taxonomies'
    name = sa.Column(
        sa.String,
        nullable=False,
        doc='Short string to make this taxonomy memorable to end users.',
    )
    hierarchy = sa.Column(
        JSONB,
        nullable=False,
        doc='Nested JSON describing the taxonomy '
        'which should be validated against '
        'a schema before entry.',
    )
    provenance = sa.Column(
        sa.String,
        nullable=True,
        doc='Identifier (e.g., URL or git hash) that '
        'uniquely ties this taxonomy back '
        'to an origin or place of record.',
    )
    version = sa.Column(
        sa.String, nullable=False, doc='Semantic version of this taxonomy'
    )

    isLatest = sa.Column(
        sa.Boolean,
        default=True,
        nullable=False,
        doc='Consider this the latest version of '
        'the taxonomy with this name? Defaults '
        'to True.',
    )
    groups = relationship(
        "Group",
        secondary="group_taxonomy",
        cascade="save-update," "merge, refresh-expire, expunge",
        passive_deletes=True,
        doc="List of Groups that have access to this Taxonomy.",
    )

    classifications = relationship(
        'Classification',
        back_populates='taxonomy',
        cascade='save-update, merge, refresh-expire, expunge',
        passive_deletes=True,
        order_by="Classification.created_at",
        doc="Classifications made within this Taxonomy.",
    )


GroupTaxonomy = join_model("group_taxonomy", Group, Taxonomy)
GroupTaxonomy.__doc__ = "Join table mapping Groups to Taxonomies."


class Comment(ReadableByGroupsMembersIfObjIsReadable, Base):
    """A comment made by a User or a Robot (via the API) on a Source."""

    text = sa.Column(sa.String, nullable=False, doc="Comment body.")
    ctype = sa.Column(
        sa.Enum('text', 'redshift', name='comment_types', validate_strings=True),
        doc="Comment type. Can be one of 'text' or 'redshift'.",
    )

    attachment_name = sa.Column(
        sa.String, nullable=True, doc="Filename of the attachment."
    )

    attachment_bytes = sa.Column(
        sa.types.LargeBinary,
        nullable=True,
        doc="Binary representation of the attachment.",
    )

    origin = sa.Column(sa.String, nullable=True, doc='Comment origin.')
    author = relationship(
        "User", back_populates="comments", doc="Comment's author.", uselist=False,
    )
    author_id = sa.Column(
        sa.ForeignKey('users.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
        doc="ID of the Comment author's User instance.",
    )
    obj_id = sa.Column(
        sa.ForeignKey('objs.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
        doc="ID of the Comment's Obj.",
    )
    obj = relationship('Obj', back_populates='comments', doc="The Comment's Obj.")
    groups = relationship(
        "Group",
        secondary="group_comments",
        cascade="save-update, merge, refresh-expire, expunge",
        passive_deletes=True,
        doc="Groups that can see the comment.",
    )

    @property
    def author_info(self):
        return {
            field: getattr(self.author, field)
            for field in ('username', 'first_name', 'last_name', 'gravatar_url')
        }

    def to_dict(self):
        _ = self.groups
        dict = super().to_dict()
        dict['author'] = self.author.to_dict()
        dict['author_info'] = self.author_info
        return dict


GroupComment = join_model("group_comments", Group, Comment)
GroupComment.__doc__ = "Join table mapping Groups to Comments."

User.comments = relationship("Comment", back_populates="author")


class Annotation(ReadableByGroupsMembersIfObjIsReadable, Base):
    """A sortable/searchable Annotation made by a filter or other robot,
    with a set of data as JSON """

    __table_args__ = (UniqueConstraint('obj_id', 'origin'),)

    data = sa.Column(JSONB, default=None, doc="Searchable data in JSON format")
    author = relationship(
        "User", back_populates="annotations", doc="Annotation's author.", uselist=False,
    )
    author_id = sa.Column(
        sa.ForeignKey('users.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
        doc="ID of the Annotation author's User instance.",
    )

    origin = sa.Column(
        sa.String,
        index=True,
        nullable=False,
        doc=(
            'What generated the annotation. This should generally map to a '
            'filter/group name. But since an annotation can be made accessible to multiple '
            'groups, the origin name does not necessarily have to map to a single group name.'
            ' The important thing is to make the origin distinct and descriptive such '
            'that annotations from the same origin generally have the same metrics. One '
            'annotation with multiple fields from each origin is allowed.'
        ),
    )
    obj_id = sa.Column(
        sa.ForeignKey('objs.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
        doc="ID of the Annotation's Obj.",
    )

    obj = relationship('Obj', back_populates='annotations', doc="The Annotation's Obj.")
    groups = relationship(
        "Group",
        secondary="group_annotations",
        cascade="save-update, merge, refresh-expire, expunge",
        passive_deletes=True,
        doc="Groups that can see the annotation.",
    )

    @property
    def author_info(self):
        return {
            field: getattr(self.author, field)
            for field in ('username', 'first_name', 'last_name', 'gravatar_url')
        }

    def to_dict(self):
        _ = self.groups
        dict = super().to_dict()
        dict['author'] = self.author.to_dict()
        dict['author_info'] = self.author_info
        return dict

    __table_args__ = (UniqueConstraint('obj_id', 'origin'),)


GroupAnnotation = join_model("group_annotations", Group, Annotation)
GroupAnnotation.__doc__ = "Join table mapping Groups to Annotation."

User.annotations = relationship("Annotation", back_populates="author")


class Classification(ReadableByGroupsMembersIfObjIsReadable, Base):
    """Classification of an Obj."""

    classification = sa.Column(sa.String, nullable=False, doc="The assigned class.")
    taxonomy_id = sa.Column(
        sa.ForeignKey('taxonomies.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
        doc="ID of the Taxonomy in which this Classification was made.",
    )
    taxonomy = relationship(
        'Taxonomy',
        back_populates='classifications',
        doc="Taxonomy in which this Classification was made.",
    )
    probability = sa.Column(
        sa.Float,
        doc='User-assigned probability of belonging to this class',
        nullable=True,
    )

    author_id = sa.Column(
        sa.ForeignKey('users.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
        doc="ID of the User that made this Classification",
    )
    author = relationship('User', doc="The User that made this classification.")
    author_name = sa.Column(
        sa.String,
        nullable=False,
        doc="User.username or Token.id " "of the Classification's author.",
    )
    obj_id = sa.Column(
        sa.ForeignKey('objs.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
        doc="ID of the Classification's Obj.",
    )
    obj = relationship(
        'Obj', back_populates='classifications', doc="The Classification's Obj."
    )
    groups = relationship(
        "Group",
        secondary="group_classifications",
        cascade="save-update, merge, refresh-expire, expunge",
        passive_deletes=True,
        doc="Groups that can access this Classification.",
    )

    def to_dict(self):
        _ = self.groups
        return super().to_dict()


GroupClassifications = join_model("group_classifications", Group, Classification)
GroupClassifications.__doc__ = "Join table mapping Groups to Classifications."


class Photometry(ha.Point, ReadableByGroupsMembers, ModifiableByOwner, Base):
    """Calibrated measurement of the flux of an object through a broadband filter."""

    __tablename__ = 'photometry'

    mjd = sa.Column(sa.Float, nullable=False, doc='MJD of the observation.', index=True)
    flux = sa.Column(
        sa.Float,
        doc='Flux of the observation in µJy. '
        'Corresponds to an AB Zeropoint of 23.9 in all '
        'filters.',
        server_default='NaN',
        nullable=False,
    )

    fluxerr = sa.Column(
        sa.Float, nullable=False, doc='Gaussian error on the flux in µJy.'
    )
    filter = sa.Column(
        allowed_bandpasses,
        nullable=False,
        doc='Filter with which the observation was taken.',
    )

    ra_unc = sa.Column(sa.Float, doc="Uncertainty of ra position [arcsec]")
    dec_unc = sa.Column(sa.Float, doc="Uncertainty of dec position [arcsec]")

    original_user_data = sa.Column(
        JSONB,
        doc='Original data passed by the user '
        'through the PhotometryHandler.POST '
        'API or the PhotometryHandler.PUT '
        'API. The schema of this JSON '
        'validates under either '
        'schema.PhotometryFlux or schema.PhotometryMag '
        '(depending on how the data was passed).',
    )
    altdata = sa.Column(JSONB, doc="Arbitrary metadata in JSON format..")
    upload_id = sa.Column(
        sa.String,
        nullable=False,
        default=lambda: str(uuid.uuid4()),
        doc="ID of the batch in which this Photometry was uploaded (for bulk deletes).",
    )
    origin = sa.Column(
        sa.String,
        nullable=False,
        unique=False,
        index=True,
        doc="Origin from which this Photometry was extracted (if any).",
        server_default='',
    )

    obj_id = sa.Column(
        sa.ForeignKey('objs.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
        doc="ID of the Photometry's Obj.",
    )
    obj = relationship('Obj', back_populates='photometry', doc="The Photometry's Obj.")
    groups = relationship(
        "Group",
        secondary="group_photometry",
        back_populates="photometry",
        cascade="save-update, merge, refresh-expire, expunge",
        passive_deletes=True,
        doc="Groups that can access this Photometry.",
    )
    instrument_id = sa.Column(
        sa.ForeignKey('instruments.id'),
        nullable=False,
        index=True,
        doc="ID of the Instrument that took this Photometry.",
    )
    instrument = relationship(
        'Instrument',
        back_populates='photometry',
        doc="Instrument that took this Photometry.",
    )

    followup_request_id = sa.Column(
        sa.ForeignKey('followuprequests.id'), nullable=True, index=True
    )
    followup_request = relationship('FollowupRequest', back_populates='photometry')

    assignment_id = sa.Column(
        sa.ForeignKey('classicalassignments.id'), nullable=True, index=True
    )
    assignment = relationship('ClassicalAssignment', back_populates='photometry')

    owner_id = sa.Column(
        sa.ForeignKey('users.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
        doc="ID of the User who uploaded the photometry.",
    )
    owner = relationship(
        'User',
        back_populates='photometry',
        foreign_keys=[owner_id],
        cascade='save-update, merge, refresh-expire, expunge',
        doc="The User who uploaded the photometry.",
    )

    @hybrid_property
    def mag(self):
        """The magnitude of the photometry point in the AB system."""
        if self.flux is not None and self.flux > 0:
            return -2.5 * np.log10(self.flux) + PHOT_ZP
        else:
            return None

    @hybrid_property
    def e_mag(self):
        """The error on the magnitude of the photometry point."""
        if self.flux is not None and self.flux > 0 and self.fluxerr > 0:
            return (2.5 / np.log(10)) * (self.fluxerr / self.flux)
        else:
            return None

    @mag.expression
    def mag(cls):
        """The magnitude of the photometry point in the AB system."""
        return sa.case(
            [
                (
                    sa.and_(cls.flux != None, cls.flux > 0),  # noqa
                    -2.5 * sa.func.log(cls.flux) + PHOT_ZP,
                )
            ],
            else_=None,
        )

    @e_mag.expression
    def e_mag(cls):
        """The error on the magnitude of the photometry point."""
        return sa.case(
            [
                (
                    sa.and_(
                        cls.flux != None, cls.flux > 0, cls.fluxerr > 0
                    ),  # noqa: E711
                    2.5 / sa.func.ln(10) * cls.fluxerr / cls.flux,
                )
            ],
            else_=None,
        )

    @hybrid_property
    def jd(self):
        """Julian Date of the exposure that produced this Photometry."""
        return self.mjd + 2_400_000.5

    @hybrid_property
    def iso(self):
        """UTC ISO timestamp (ArrowType) of the exposure that produced this Photometry."""
        return arrow.get((self.mjd - 40_587) * 86400.0)

    @iso.expression
    def iso(cls):
        """UTC ISO timestamp (ArrowType) of the exposure that produced this Photometry."""
        # converts MJD to unix timestamp
        return sa.func.to_timestamp((cls.mjd - 40_587) * 86400.0)

    @hybrid_property
    def snr(self):
        """Signal-to-noise ratio of this Photometry point."""
        return self.flux / self.fluxerr if self.flux and self.fluxerr else None

    @snr.expression
    def snr(self):
        """Signal-to-noise ratio of this Photometry point."""
        return self.flux / self.fluxerr


# Deduplication index. This is a unique index that prevents any photometry
# point that has the same obj_id, instrument_id, origin, mjd, flux error,
# and flux as a photometry point that already exists within the table from
# being inserted into the table. The index also allows fast lookups on this
# set of columns, making the search for duplicates a O(log(n)) operation.

Photometry.__table_args__ = (
    sa.Index(
        'deduplication_index',
        Photometry.obj_id,
        Photometry.instrument_id,
        Photometry.origin,
        Photometry.mjd,
        Photometry.fluxerr,
        Photometry.flux,
        unique=True,
    ),
)


User.photometry = relationship(
    'Photometry', doc='Photometry uploaded by this User.', back_populates='owner'
)

GroupPhotometry = join_model("group_photometry", Group, Photometry)
GroupPhotometry.__doc__ = "Join table mapping Groups to Photometry."


class Spectrum(ReadableByGroupsMembersIfObjIsReadable, ModifiableByOwner, Base):
    """Wavelength-dependent measurement of the flux of an object through a
    dispersive element."""

    __tablename__ = 'spectra'
    # TODO better numpy integration
    wavelengths = sa.Column(
        NumpyArray, nullable=False, doc="Wavelengths of the spectrum [Angstrom]."
    )
    fluxes = sa.Column(
        NumpyArray,
        nullable=False,
        doc="Flux of the Spectrum [F_lambda, arbitrary units].",
    )
    errors = sa.Column(
        NumpyArray,
        doc="Errors on the fluxes of the spectrum [F_lambda, same units as `fluxes`.]",
    )

    obj_id = sa.Column(
        sa.ForeignKey('objs.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
        doc="ID of this Spectrum's Obj.",
    )
    obj = relationship('Obj', back_populates='spectra', doc="The Spectrum's Obj.")
    observed_at = sa.Column(
        sa.DateTime,
        nullable=False,
        doc="Median UTC ISO time stamp of the exposure or exposures in which the Spectrum was acquired.",
    )
    origin = sa.Column(sa.String, nullable=True, doc="Origin of the spectrum.")
    # TODO program?
    instrument_id = sa.Column(
        sa.ForeignKey('instruments.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
        doc="ID of the Instrument that acquired the Spectrum.",
    )
    instrument = relationship(
        'Instrument',
        back_populates='spectra',
        doc="The Instrument that acquired the Spectrum.",
    )
    groups = relationship(
        "Group",
        secondary="group_spectra",
        back_populates="spectra",
        cascade="save-update, merge, refresh-expire, expunge",
        passive_deletes=True,
        doc='Groups that can view this spectrum.',
    )

    reducers = relationship(
        "User", secondary="spectrum_reducers", doc="Users that reduced this spectrum."
    )
    observers = relationship(
        "User", secondary="spectrum_observers", doc="Users that observed this spectrum."
    )

    followup_request_id = sa.Column(sa.ForeignKey('followuprequests.id'), nullable=True)
    followup_request = relationship('FollowupRequest', back_populates='spectra')

    assignment_id = sa.Column(sa.ForeignKey('classicalassignments.id'), nullable=True)
    assignment = relationship('ClassicalAssignment', back_populates='spectra')

    altdata = sa.Column(
        psql.JSONB, doc="Miscellaneous alternative metadata.", nullable=True
    )

    original_file_string = sa.Column(
        sa.String,
        doc="Content of original file that was passed to upload the spectrum.",
    )
    original_file_filename = sa.Column(
        sa.String, doc="Original file name that was passed to upload the spectrum."
    )

    owner_id = sa.Column(
        sa.ForeignKey('users.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
        doc="ID of the User who uploaded the spectrum.",
    )
    owner = relationship(
        'User',
        back_populates='spectra',
        foreign_keys=[owner_id],
        cascade='save-update, merge, refresh-expire, expunge',
        doc="The User who uploaded the spectrum.",
    )

    @classmethod
    def from_ascii(
        cls,
        file,
        obj_id=None,
        instrument_id=None,
        observed_at=None,
        wave_column=0,
        flux_column=1,
        fluxerr_column=None,
    ):
        """Generate a `Spectrum` from an ascii file.

        Parameters
        ----------
        file : str or file-like object
           Name or handle of the ASCII file containing the spectrum.
        obj_id : str
           The id of the Obj that this Spectrum is of, if not present
           in the ASCII header.
        instrument_id : int
           ID of the Instrument with which this Spectrum was acquired,
           if not present in the ASCII header.
        observed_at : string or datetime
           Median UTC ISO time stamp of the exposure or exposures in which
           the Spectrum was acquired, if not present in the ASCII header.
        wave_column: integer, optional
           The 0-based index of the ASCII column corresponding to the wavelength
           values of the spectrum (default 0).
        flux_column: integer, optional
           The 0-based index of the ASCII column corresponding to the flux
           values of the spectrum (default 1).
        fluxerr_column: integer or None, optional
           The 0-based index of the ASCII column corresponding to the flux error
           values of the spectrum (default None).
        Returns
        -------
        spec : `skyportal.models.Spectrum`
           The Spectrum generated from the ASCII file.

        """

        try:
            f = open(file, 'rb')  # read as ascii
        except TypeError:
            # it's already a stream
            f = file

        try:
            table = ascii.read(f, comment='#', header_start=None)
        except Exception as e:
            e.args = (f'Error parsing ASCII file: {e.args[0]}',)
            raise
        finally:
            f.close()

        tabledata = np.asarray(table)
        colnames = table.colnames

        # validate the table and some of the input parameters

        # require at least 2 columns (wavelength, flux)
        ncol = len(colnames)
        if ncol < 2:
            raise ValueError(
                'Input data must have at least 2 columns (wavelength, '
                'flux, and optionally flux error).'
            )

        spec_data = {}
        # validate the column indices
        for index, name, dbcol in zip(
            [wave_column, flux_column, fluxerr_column],
            ['wave_column', 'flux_column', 'fluxerr_column'],
            ['wavelengths', 'fluxes', 'errors'],
        ):

            # index format / type validation:
            if dbcol in ['wavelengths', 'fluxes']:
                if not isinstance(index, int):
                    raise ValueError(f'{name} must be an int')
            else:
                if index is not None and not isinstance(index, int):
                    # The only other allowed value is that fluxerr_column can be
                    # None. If the value of index is not None, raise.
                    raise ValueError(f'invalid type for {name}')

            # after validating the indices, ensure that the columns they
            # point to exist
            if isinstance(index, int):
                if index >= ncol:
                    raise ValueError(
                        f'index {name} ({index}) is greater than the '
                        f'maximum allowed value ({ncol - 1})'
                    )
                spec_data[dbcol] = tabledata[colnames[index]].astype(float)

        # parse the header
        if 'comments' in table.meta:

            # this section matches lines like:
            # XTENSION: IMAGE
            # BITPIX: -32
            # NAXIS: 2
            # NAXIS1: 433
            # NAXIS2: 1

            header = {}
            for line in table.meta['comments']:
                try:
                    result = yaml.load(line, Loader=yaml.FullLoader)
                except yaml.YAMLError:
                    continue
                if isinstance(result, dict):
                    header.update(result)

            # this section matches lines like:
            # FILTER  = 'clear   '           / Filter
            # EXPTIME =              600.003 / Total exposure time (sec); avg. of R&B
            # OBJECT  = 'ZTF20abpuxna'       / User-specified object name
            # TARGNAME= 'ZTF20abpuxna_S1'    / Target name (from starlist)
            # DICHNAME= '560     '           / Dichroic

            cards = []
            with warnings.catch_warnings():
                warnings.simplefilter('error', AstropyWarning)
                for line in table.meta['comments']:
                    # this line does not raise a warning
                    card = fits.Card.fromstring(line)
                    try:
                        # this line warns (exception in this context)
                        card.verify()
                    except AstropyWarning:
                        continue
                    cards.append(card)

            # this ensures lines like COMMENT and HISTORY are properly dealt
            # with by using the astropy.header machinery to coerce them to
            # single strings

            fits_header = fits.Header(cards=cards)
            serialized = dict(fits_header)

            commentary_keywords = ['', 'COMMENT', 'HISTORY', 'END']

            for key in serialized:
                # coerce things to serializable JSON
                if key in commentary_keywords:
                    # serialize as a string - otherwise it returns a
                    # funky astropy type that is not json serializable
                    serialized[key] = str(serialized[key])

                if len(fits_header.comments[key]) > 0:
                    header[key] = {
                        'value': serialized[key],
                        'comment': fits_header.comments[key],
                    }
                else:
                    header[key] = serialized[key]

            # this ensures that the spectra are properly serialized to the
            # database JSONB (database JSONB cant handle datetime/date values)
            header = json.loads(to_json(header))

        else:
            header = None

        return cls(
            obj_id=obj_id,
            instrument_id=instrument_id,
            observed_at=observed_at,
            altdata=header,
            **spec_data,
        )


User.spectra = relationship(
    'Spectrum', doc='Spectra uploaded by this User.', back_populates='owner'
)

SpectrumReducer = join_model("spectrum_reducers", Spectrum, User)
SpectrumObserver = join_model("spectrum_observers", Spectrum, User)

GroupSpectrum = join_model("group_spectra", Group, Spectrum)
GroupSpectrum.__doc__ = 'Join table mapping Groups to Spectra.'


# def format_public_url(context):
#    """TODO migrate this to broker tools"""
#    file_uri = context.current_parameters.get('file_uri')
#    if file_uri is None:
#        return None
#    elif file_uri.startswith('s3'):  # TODO is this reliable?
#        raise NotImplementedError
#    elif file_uri.startswith('http://'): # TODO is this reliable?
#        return file_uri
#    else:  # local file
#        return '/' + file_uri.lstrip('./')


class FollowupRequest(ReadableIfObjIsReadable, WriteProtected, Base):
    """A request for follow-up data (spectroscopy, photometry, or both) using a
    robotic instrument."""

    requester_id = sa.Column(
        sa.ForeignKey('users.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
        doc="ID of the User who requested the follow-up.",
    )

    requester = relationship(
        User,
        back_populates='followup_requests',
        doc="The User who requested the follow-up.",
        foreign_keys=[requester_id],
    )

    last_modified_by_id = sa.Column(
        sa.ForeignKey('users.id', ondelete='SET NULL'),
        nullable=False,
        doc="The ID of the User who last modified the request.",
    )

    last_modified_by = relationship(
        User,
        doc="The user who last modified the request.",
        foreign_keys=[last_modified_by_id],
    )

    obj = relationship('Obj', back_populates='followup_requests', doc="The target Obj.")
    obj_id = sa.Column(
        sa.ForeignKey('objs.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
        doc="ID of the target Obj.",
    )

    payload = sa.Column(
        psql.JSONB, nullable=False, doc="Content of the followup request."
    )

    status = sa.Column(
        sa.String(),
        nullable=False,
        default="pending submission",
        index=True,
        doc="The status of the request.",
    )

    allocation_id = sa.Column(
        sa.ForeignKey('allocations.id', ondelete='CASCADE'), nullable=False, index=True
    )
    allocation = relationship('Allocation', back_populates='requests')

    transactions = relationship(
        'FacilityTransaction',
        back_populates='followup_request',
        order_by="FacilityTransaction.created_at.desc()",
    )

    target_groups = relationship(
        'Group',
        secondary='request_groups',
        passive_deletes=True,
        doc='Groups to share the resulting data from this request with.',
    )

    photometry = relationship('Photometry', back_populates='followup_request')
    spectra = relationship('Spectrum', back_populates='followup_request')

    @property
    def instrument(self):
        return self.allocation.instrument


FollowupRequestTargetGroup = join_model('request_groups', FollowupRequest, Group)


class FacilityTransaction(Base):

    created_at = sa.Column(
        sa.DateTime,
        nullable=False,
        default=datetime.utcnow,
        index=True,
        doc="UTC time this FacilityTransaction was created.",
    )

    request = sa.Column(psql.JSONB, doc='Serialized HTTP request.')
    response = sa.Column(psql.JSONB, doc='Serialized HTTP response.')

    followup_request_id = sa.Column(
        sa.ForeignKey('followuprequests.id', ondelete='CASCADE'),
        index=True,
        nullable=False,
        doc="The ID of the FollowupRequest this message pertains to.",
    )

    followup_request = relationship(
        'FollowupRequest',
        back_populates='transactions',
        doc="The FollowupRequest this message pertains to.",
    )

    initiator_id = sa.Column(
        sa.ForeignKey('users.id', ondelete='SET NULL'),
        index=True,
        nullable=False,
        doc='The ID of the User who initiated the transaction.',
    )
    initiator = relationship(
        'User',
        back_populates='transactions',
        doc='The User who initiated the transaction.',
    )


User.followup_requests = relationship(
    'FollowupRequest',
    back_populates='requester',
    doc="The follow-up requests this User has made.",
    foreign_keys=[FollowupRequest.requester_id],
)

User.transactions = relationship(
    'FacilityTransaction',
    back_populates='initiator',
    doc="The FacilityTransactions initiated by this User.",
)


class Thumbnail(ReadableIfObjIsReadable, Base):
    """Thumbnail image centered on the location of an Obj."""

    # TODO delete file after deleting row
    type = sa.Column(
        thumbnail_types, doc='Thumbnail type (e.g., ref, new, sub, dr8, ps1, ...)'
    )
    file_uri = sa.Column(
        sa.String(),
        nullable=True,
        index=False,
        unique=False,
        doc="Path of the Thumbnail on the machine running SkyPortal.",
    )
    public_url = sa.Column(
        sa.String(),
        nullable=True,
        index=False,
        unique=False,
        doc="Publically accessible URL of the thumbnail.",
    )
    origin = sa.Column(sa.String, nullable=True, doc="Origin of the Thumbnail.")
    obj = relationship(
        'Obj', back_populates='thumbnails', uselist=False, doc="The Thumbnail's Obj.",
    )
    obj_id = sa.Column(
        sa.ForeignKey('objs.id', ondelete='CASCADE'),
        index=True,
        nullable=False,
        doc="ID of the thumbnail's obj.",
    )


class ObservingRun(ModifiableByOwner, Base):
    """A classical observing run with a target list (of Objs)."""

    instrument_id = sa.Column(
        sa.ForeignKey('instruments.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
        doc="ID of the Instrument used for this run.",
    )
    instrument = relationship(
        'Instrument',
        cascade='save-update, merge, refresh-expire, expunge',
        uselist=False,
        back_populates='observing_runs',
        doc="The Instrument for this run.",
    )

    # name of the PI
    pi = sa.Column(sa.String, doc="The name(s) of the PI(s) of this run.")
    observers = sa.Column(sa.String, doc="The name(s) of the observer(s) on this run.")

    sources = relationship(
        'Obj',
        secondary='join(ClassicalAssignment, Obj)',
        cascade='save-update, merge, refresh-expire, expunge',
        passive_deletes=True,
        doc="The targets [Objs] for this run.",
    )

    # let this be nullable to accommodate external groups' runs
    group = relationship(
        'Group',
        back_populates='observing_runs',
        doc='The Group associated with this Run.',
    )
    group_id = sa.Column(
        sa.ForeignKey('groups.id', ondelete='CASCADE'),
        nullable=True,
        index=True,
        doc='The ID of the Group associated with this run.',
    )

    # the person who uploaded the run
    owner = relationship(
        'User',
        back_populates='observing_runs',
        doc="The User who created this ObservingRun.",
    )
    owner_id = sa.Column(
        sa.ForeignKey('users.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
        doc="The ID of the User who created this ObservingRun.",
    )

    assignments = relationship(
        'ClassicalAssignment',
        passive_deletes=True,
        doc="The Target Assignments for this Run.",
    )
    calendar_date = sa.Column(
        sa.Date, nullable=False, index=True, doc="The Local Calendar date of this Run."
    )

    @property
    def calendar_noon(self):
        observer = self.instrument.telescope.observer
        year = self.calendar_date.year
        month = self.calendar_date.month
        day = self.calendar_date.day
        hour = 12
        noon = datetime(
            year=year, month=month, day=day, hour=hour, tzinfo=observer.timezone
        )
        noon = noon.astimezone(timezone.utc).timestamp()
        noon = ap_time.Time(noon, format='unix')
        return noon

    def rise_time(self, target_or_targets, altitude=30 * u.degree):
        """The rise time of the specified targets as an astropy.time.Time."""
        observer = self.instrument.telescope.observer
        sunset = self.instrument.telescope.next_sunset(self.calendar_noon).reshape((1,))
        sunrise = self.instrument.telescope.next_sunrise(self.calendar_noon).reshape(
            (1,)
        )
        original_shape = np.asarray(target_or_targets).shape
        target_array = (
            [target_or_targets] if len(original_shape) == 0 else target_or_targets
        )

        next_rise = observer.target_rise_time(
            sunset, target_array, which='next', horizon=altitude
        ).reshape((len(target_array),))

        # if next rise time is after next sunrise, the target rises before
        # sunset. show the previous rise so that the target is shown to be
        # "already up" when the run begins (a beginning of night target).

        recalc = next_rise > sunrise
        if recalc.any():
            target_subarr = [t for t, b in zip(target_array, recalc) if b]
            next_rise[recalc] = observer.target_rise_time(
                sunset, target_subarr, which='previous', horizon=altitude
            ).reshape((len(target_subarr),))

        return next_rise.reshape(original_shape)

    def set_time(self, target_or_targets, altitude=30 * u.degree):
        """The set time of the specified targets as an astropy.time.Time."""
        observer = self.instrument.telescope.observer
        sunset = self.instrument.telescope.next_sunset(self.calendar_noon)
        original_shape = np.asarray(target_or_targets).shape
        return observer.target_set_time(
            sunset, target_or_targets, which='next', horizon=altitude
        ).reshape(original_shape)


User.observing_runs = relationship(
    'ObservingRun',
    cascade='save-update, merge, refresh-expire, expunge',
    doc="Observing Runs this User has created.",
)


class ClassicalAssignment(ReadableIfObjIsReadable, WriteProtected, Base):
    """Assignment of an Obj to an Observing Run as a target."""

    requester_id = sa.Column(
        sa.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        doc="The ID of the User who created this assignment.",
    )
    requester = relationship(
        "User",
        back_populates="assignments",
        foreign_keys=[requester_id],
        doc="The User who created this assignment.",
    )

    last_modified_by_id = sa.Column(
        sa.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        default=None,
        index=True,
    )
    last_modified_by = relationship("User", foreign_keys=[last_modified_by_id])

    obj = relationship('Obj', back_populates='assignments', doc='The assigned Obj.')
    obj_id = sa.Column(
        sa.ForeignKey('objs.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
        doc='ID of the assigned Obj.',
    )

    comment = sa.Column(
        sa.String(),
        doc="A comment on the assignment. "
        "Typically a justification for the request, "
        "or instructions for taking the data.",
    )
    status = sa.Column(
        sa.String(),
        nullable=False,
        default="pending",
        doc='Status of the assignment [done, not done, pending].',
    )
    priority = sa.Column(
        followup_priorities,
        nullable=False,
        doc='Priority of the request (1 = lowest, 5 = highest).',
    )
    spectra = relationship(
        "Spectrum",
        back_populates="assignment",
        doc="Spectra produced by the assignment.",
    )
    photometry = relationship(
        "Photometry",
        back_populates="assignment",
        doc="Photometry produced by the assignment.",
    )

    run = relationship(
        'ObservingRun',
        back_populates='assignments',
        doc="The ObservingRun this target was assigned to.",
    )
    run_id = sa.Column(
        sa.ForeignKey('observingruns.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
        doc="ID of the ObservingRun this target was assigned to.",
    )

    @hybrid_property
    def instrument(self):
        """The instrument in use on the assigned ObservingRun."""
        return self.run.instrument

    @property
    def rise_time(self):
        """The UTC time at which the object rises on this run."""
        target = self.obj.target
        return self.run.rise_time(target)

    @property
    def set_time(self):
        """The UTC time at which the object sets on this run."""
        target = self.obj.target
        return self.run.set_time(target)


User.assignments = relationship(
    'ClassicalAssignment',
    back_populates='requester',
    doc="Objs the User has assigned to ObservingRuns.",
    foreign_keys="ClassicalAssignment.requester_id",
)


class Invitation(Base):
    token = sa.Column(sa.String(), nullable=False, unique=True)
    groups = relationship(
        "Group",
        secondary="group_invitations",
        cascade="save-update, merge, refresh-expire, expunge",
        passive_deletes=True,
    )
    streams = relationship(
        "Stream",
        secondary="stream_invitations",
        cascade="save-update, merge, refresh-expire, expunge",
        passive_deletes=True,
    )
    admin_for_groups = sa.Column(psql.ARRAY(sa.Boolean), nullable=False)
    user_email = sa.Column(EmailType(), nullable=True)
    invited_by = relationship(
        "User",
        secondary="user_invitations",
        cascade="save-update, merge, refresh-expire, expunge",
        passive_deletes=True,
        uselist=False,
    )
    used = sa.Column(sa.Boolean, nullable=False, default=False)


GroupInvitation = join_model('group_invitations', Group, Invitation)
StreamInvitation = join_model('stream_invitations', Stream, Invitation)
UserInvitation = join_model("user_invitations", User, Invitation)


@event.listens_for(Invitation, 'after_insert')
def send_user_invite_email(mapper, connection, target):
    app_base_url = get_app_base_url()
    link_location = f'{app_base_url}/login/google-oauth2/?invite_token={target.token}'
    send_email(
        recipients=[target.user_email],
        subject=cfg["invitations.email_subject"],
        body=(
            f'{cfg["invitations.email_body_preamble"]}<br /><br />'
            f'Please click <a href="{link_location}">here</a> to join.'
        ),
    )


class SourceNotification(ReadableByGroupsMembers, Base):
    groups = relationship(
        "Group",
        secondary="group_notifications",
        cascade="save-update, merge, refresh-expire, expunge",
        passive_deletes=True,
    )
    sent_by_id = sa.Column(
        sa.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        doc="The ID of the User who sent this notification.",
    )
    sent_by = relationship(
        "User",
        back_populates="source_notifications",
        foreign_keys=[sent_by_id],
        doc="The User who sent this notification.",
    )
    source_id = sa.Column(
        sa.ForeignKey("objs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        doc="ID of the target Obj.",
    )
    source = relationship(
        'Obj', back_populates='obj_notifications', doc='The target Obj.'
    )

    additional_notes = sa.Column(sa.String(), nullable=True)
    level = sa.Column(sa.String(), nullable=False)


GroupSourceNotification = join_model('group_notifications', Group, SourceNotification)
User.source_notifications = relationship(
    'SourceNotification',
    back_populates='sent_by',
    doc="Source notifications the User has sent out.",
    foreign_keys="SourceNotification.sent_by_id",
)


@event.listens_for(SourceNotification, 'after_insert')
def send_source_notification(mapper, connection, target):
    app_base_url = get_app_base_url()

    link_location = f'{app_base_url}/source/{target.source_id}'
    if target.sent_by.first_name is not None and target.sent_by.last_name is not None:
        sent_by_name = f'{target.sent_by.first_name} {target.sent_by.last_name}'
    else:
        sent_by_name = target.sent_by.username

    group_ids = map(lambda group: group.id, target.groups)
    groups = DBSession().query(Group).filter(Group.id.in_(group_ids)).all()

    target_users = set()
    for group in groups:
        # Use a set to get unique iterable of users
        target_users.update(group.users)

    source = DBSession().query(Obj).get(target.source_id)
    source_info = ""
    if source.ra is not None:
        source_info += f'RA={source.ra} '
    if source.dec is not None:
        source_info += f'Dec={source.dec}'
    source_info = source_info.strip()

    # Send SMS messages to opted-in users if desired
    if target.level == "hard":
        message_text = (
            f'{cfg["app.title"]}: {sent_by_name} would like to call your immediate'
            f' attention to a source at {link_location} ({source_info}).'
        )
        if target.additional_notes != "" and target.additional_notes is not None:
            message_text += f' Addtional notes: {target.additional_notes}'

        account_sid = cfg["twilio.sms_account_sid"]
        auth_token = cfg["twilio.sms_auth_token"]
        from_number = cfg["twilio.from_number"]
        client = TwilioClient(account_sid, auth_token)
        for user in target_users:
            # If user has a phone number registered and opted into SMS notifications
            if (
                user.contact_phone is not None
                and user.preferences is not None
                and "allowSMSAlerts" in user.preferences
                and user.preferences.get("allowSMSAlerts")
            ):
                client.messages.create(
                    body=message_text, from_=from_number, to=user.contact_phone.e164
                )

    # Send email notifications
    recipients = []
    for user in target_users:
        # If user has a contact email registered and opted into email notifications
        if (
            user.contact_email is not None
            and user.preferences is not None
            and "allowEmailAlerts" in user.preferences
            and user.preferences.get("allowEmailAlerts")
        ):
            recipients.append(user.contact_email)

    descriptor = "immediate" if target.level == "hard" else ""
    html_content = (
        f'{sent_by_name} would like to call your {descriptor} attention to'
        f' <a href="{link_location}">{target.source_id}</a> ({source_info})'
    )
    if target.additional_notes != "" and target.additional_notes is not None:
        html_content += f'<br /><br />Additional notes: {target.additional_notes}'

    if len(recipients) > 0:
        send_email(
            recipients=recipients,
            subject=f'{cfg["app.title"]}: Source Alert',
            body=html_content,
        )


@event.listens_for(User, 'after_insert')
def create_single_user_group(mapper, connection, target):

    # Create single-user group
    @event.listens_for(DBSession(), "after_flush", once=True)
    def receive_after_flush(session, context):
        session.add(
            Group(name=slugify(target.username), users=[target], single_user_group=True)
        )


@event.listens_for(User, 'before_delete')
def delete_single_user_group(mapper, connection, target):

    # Delete single-user group
    DBSession().delete(target.single_user_group)


@event.listens_for(User, 'after_update')
def update_single_user_group(mapper, connection, target):

    # Update single user group name if needed
    @event.listens_for(DBSession(), "after_flush_postexec", once=True)
    def receive_after_flush(session, context):
        single_user_group = target.single_user_group
        single_user_group.name = slugify(target.username)
        DBSession().add(single_user_group)


def _make_retreive_accessible_children(cls):
    return lambda self, user_or_token, options=[]: (
        DBSession()
        .query(cls)
        .filter(cls.obj_id == self.id, cls.is_readable_by(user_or_token))
        .options(options)
        .all()
    )


for cls in [Comment, Classification, Spectrum, Photometry, Annotation, Thumbnail]:
    func_name = f'get_{cls.__tablename__}_readable_by'
    setattr(Obj, func_name, _make_retreive_accessible_children(cls))


schema.setup_schema()
