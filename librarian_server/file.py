# -*- mode: python; coding: utf-8 -*-
# Copyright 2016-2017 the HERA Collaboration
# Licensed under the BSD License.

"Files."



__all__ = str('''
File
FileInstance
FileEvent
''').split()

import sys
import datetime
import json
import os.path
import re
from flask import flash, redirect, render_template, url_for

from . import app, db, logger
from .dbutil import NotNull, SQLAlchemyError
from .webutil import ServerError, json_api, login_required, optional_arg, required_arg
from .observation import Observation
from .store import Store


def infer_file_obsid(parent_dirs, name, info):
    """Infer the obsid associated with a file based on the limited information we
    have about it. Raises an exception if this cannot be done *with
    certainty*.

    The "hera" mode does this by looking for existing files whose names start
    with the same "zen.JD" prefix.

    The "none" mode refuses to do this.

    There is also a secret "_testing" mode.

    """
    mode = app.config.get('obsid_inference_mode', 'none')

    if mode == 'none':
        raise ServerError('refusing to try to infer the obsid of candidate new file \"%s\"', name)

    if mode == 'hera':
        bits = name.split('.')
        if len(bits) < 4:
            raise ServerError(
                'need to infer obsid of HERA file \"%s\", but its name looks weird', name)

        prefix = '.'.join(bits[:3])
        obsids = list(db.session.query(File.obsid)
                      .filter(File.name.like(prefix + '.%'))
                      .group_by(File.obsid))

        if len(obsids) != 1:
            raise ServerError('need to infer obsid of HERA file \"%s\", but got %d candidate '
                              'obsids from similarly-named files', name, len(obsids))

        return obsids[0]

    if mode == '_testing':
        bits = name.split('.')
        if len(bits) < 4:
            raise ServerError(
                'need to infer obsid of _testing file \"%s\", but its name looks weird', name)

        jd = float(bits[1] + '.' + bits[2])
        from astropy.time import Time
        from math import floor
        return int(floor(Time(jd, format='jd', scale='utc').gps))

    raise ServerError('configuration problem: unknown "obsid_inference_mode" setting %r', mode)


class File (db.Model):
    """A File describes a data product generated by HERA.

    The information described in a File structure never changes, and is
    universal between Librarians. Actual "instances" of files come and go, but
    a File record should never be deleted. The only exception to this is the
    "source" column, which is Librarian-dependent.

    A File may represent an actual flat file or a directory tree. The latter
    use case is important for MIRIAD data files, which are directories, and
    which we want to store in their native form for rapid analysis.

    File names are unique. Here, the "name" is a Unix 'basename', i.e. it
    contains no directory components or slashes. Every new file must have a
    unique new name.

    """
    __tablename__ = 'file'

    name = db.Column(db.String(256), primary_key=True)
    type = NotNull(db.String(32))
    create_time = NotNull(db.DateTime)  # rounded to integer seconds
    obsid = db.Column(db.BigInteger, db.ForeignKey(Observation.obsid), nullable=True)
    size = NotNull(db.BigInteger)
    md5 = NotNull(db.String(32))

    source = NotNull(db.String(64))
    observation = db.relationship('Observation', back_populates='files')
    instances = db.relationship('FileInstance', back_populates='file')
    events = db.relationship('FileEvent', back_populates='file')

    def __init__(self, name, type, obsid, source, size, md5, create_time=None):
        if create_time is None:
            # We round our times to whole seconds so that they can be
            # accurately represented as integer Unix times, just in case
            # floating-point rounding could sneak in as an issue.
            create_time = datetime.datetime.utcnow().replace(microsecond=0)

        from hera_librarian import utils
        md5 = utils.normalize_and_validate_md5(md5)

        self.name = name
        self.type = type
        self.create_time = create_time
        self.obsid = obsid
        self.source = source
        self.size = size
        self.md5 = md5
        self._validate()

    @property
    def name_as_json(self):
        import json
        return json.dumps(self.name)

    def _validate(self):
        """Check that this object's fields follow our invariants.

        """
        from hera_librarian import utils

        if '/' in self.name:
            raise ValueError('illegal file name "%s": names may not contain "/"' % self.name)

        utils.normalize_and_validate_md5(self.md5)

        if not (self.size >= 0):  # catches NaNs, just in case ...
            raise ValueError('illegal size %d of file "%s": negative' % (self.size, self.name))

    @classmethod
    def get_inferring_info(cls, store, store_path, source_name, info=None, null_obsid=False):
        """Get a File instance based on a file currently located in a store. We infer
        the file's properties and those of any dependent database records
        (Observation, ObservingSession), which means that we can only do this
        for certain kinds of files whose formats we understand.

        If new File and Observation records need to be created in the DB, that
        is done. If *info* is given, we use it; otherwise we SSH into the
        store to gather the info ourselves.

        If *null_obsid* is True, the entry is expected and required to have a
        null obsid. If False (the default), the file must have an obsid --
        either explicitly specified, or inferred from the file contents.

        """
        parent_dirs = os.path.dirname(store_path)
        name = os.path.basename(store_path)

        prev = cls.query.get(name)
        if prev is not None:
            # If there's already a record for this File name, then its corresponding
            # Observation etc must already be available. Let's leave well enough alone:
            return prev

        # Darn. We're going to have to create the File, and maybe its
        # Observation too. Get to it.

        if info is None:
            try:
                info = store.get_info_for_path(store_path)
            except Exception as e:
                raise ServerError('cannot register %s:%s: %s', store.name, store_path, e)

        size = required_arg(info, int, 'size')
        md5 = required_arg(info, str, 'md5')
        type = required_arg(info, str, 'type')

        from .observation import Observation
        from . import mc_integration as MC

        obsid = optional_arg(info, int, 'obsid')

        if null_obsid:
            if obsid is not None:
                raise ServerError('new file %s is expected to have a null obsid, but it has %r',
                                  name, obsid)
        else:
            if obsid is None:
                # Our UV data files embed their obsids in a way that we can
                # extract robustly, but we want to be able to ingest new files
                # that don't necessarily have obsid information embedded. We used
                # to do this by guessing from the JD in the filename, but that
                # proved to be unreliable (as you might guess). So we now have a
                # configurable scheme to make this possible; the only implemented
                # technique still looks at filenames, but does it in a somewhat
                # better-justified way where it requires preexisting files to have
                # an assigned obsid that it can copy.
                obsid = infer_file_obsid(parent_dirs, name, info)

            obs = Observation.query.get(obsid)

            if obs is None:
                # The other piece of the puzzle is that we used to sometimes
                # create new Observation records based on data that we tried to
                # infer from standalone files. Now that the we have an on-site M&C
                # system that records the canonical metadata for observations,
                # that mode is deprecated. On-site, we only create Observations
                # from M&C. Off-site, we only get them from uploads from other
                # Librarians.
                MC.create_observation_record(obsid)

        fobj = File(name, type, obsid, source_name, size, md5)

        if MC.is_file_record_invalid(fobj):
            raise ServerError('new file %s (obsid %s) rejected by M&C; see M&C error logs for the reason',
                              name, obsid)

        db.session.add(fobj)

        try:
            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
            app.log_exception(sys.exc_info())
            raise ServerError('failed to add new file %s to database; see logs for details', name)

        MC.note_file_created(fobj)

        return fobj

    def delete_instances(self, mode='standard', restrict_to_store=None):
        """DANGER ZONE! Delete instances of this file on all stores!

        We have a safety interlock: each FileInstance has a "deletion_policy"
        flag that specifies, well, the internal policy about whether it can be
        deleted. The default is that no deletions are allowed.

        Of course, this command will only execute deletions that are allowed
        under the policy. It returns status information about how many
        deletions actually occurred.

        If `mode` is "noop", the logic is exercised but the deletions are not
        run. Currently, the only other allowed mode is "standard".

        If `restrict_to_store` is not None, it should be a Store class
        instance. Only instances kept on the specified store will be deleted
        -- all other instances will be kept.

        """
        if mode == 'standard':
            noop = False
        elif mode == 'noop':
            noop = True
        else:
            raise ServerError('unexpected deletion operations mode %r' % (mode,))

        n_deleted = 0
        n_kept = 0
        n_error = 0

        # If we make Librarian files read-only, we'll need to chmod them back
        # to writeable before we can blow them away -- this wouldn't be
        # necessary if we only stored flat files, but we store directories
        # too.
        pmode = app.config.get('permissions_mode', 'readonly')
        need_chmod = (pmode == 'readonly')

        for inst in self.instances:
            # Currently, the policy is just binary: allowed, or not. Be very
            # careful about changing the logic here, since this is the core of
            # the safety interlock that prevents us from accidentally blowing
            # away the entire data archive! Don't be That Guy or That Gal!

            if inst.deletion_policy != DeletionPolicy.ALLOWED:
                n_kept += 1
                continue

            # Implement the `restrict_to_store` feature. We could move this
            # into the SQL query (implicit in `self.instances` above) but meh,
            # this keeps things more uniform regarding n_kept etc.

            if restrict_to_store is not None and inst.store != restrict_to_store.id:
                n_kept += 1
                continue

            # OK. If we've gotten here, we are 100% sure that it is OK to delete
            # this instance.

            store = inst.store_object

            try:
                if noop:
                    logger.info('NOOP-delete call matched instance "%s"', inst.descriptive_name())
                else:
                    logger.info('attempting to delete instance "%s"', inst.descriptive_name())
                    store._delete(inst.store_path, chmod_before=need_chmod)
            except Exception as e:
                # This could happen if we can't SSH to the store or something.
                # Safest course of action seems to be to not modify the database
                # or anything else.
                n_error += 1
                logger.warn('failed to delete instance "%s": %s', inst.descriptive_name(), e)
                continue

            # Looks like we succeeded in blowing it away.

            if not noop:
                db.session.add(self.make_instance_deletion_event(inst, store))
                db.session.delete(inst)
            n_deleted += 1

        if not noop:
            try:
                db.session.commit()
            except SQLAlchemyError:
                db.session.rollback()
                app.log_exception(sys.exc_info())
                raise ServerError(
                    'deleted instances but failed to update database! DB/FS consistency broken!')

        return {
            'n_deleted': n_deleted,
            'n_kept': n_kept,
            'n_error': n_error,
        }

    @property
    def create_time_unix(self):
        import calendar
        return calendar.timegm(self.create_time.timetuple())

    @property
    def create_time_astropy(self):
        from astropy.time import Time
        return Time(self.create_time)

    def to_dict(self):
        """Note that 'source' is not a propagated quantity, and that we explicitly
        include the null 'obsid' if that is the case.

        """
        return dict(
            name=self.name,
            type=self.type,
            create_time=self.create_time_unix,
            obsid=self.obsid,
            size=self.size,
            md5=self.md5
        )

    @classmethod
    def from_dict(cls, source, info):
        name = required_arg(info, str, 'name')
        type = required_arg(info, str, 'type')
        ctime_unix = required_arg(info, int, 'create_time')
        size = required_arg(info, int, 'size')
        md5 = required_arg(info, str, 'md5')

        # obsid needs special handling: it must be present, but it can be None.
        try:
            obsid = info['obsid']
        except KeyError:
            raise ServerError('required parameter "obsid" not provided')

        if obsid is not None and not isinstance(obsid, int):
            raise ServerError('parameter "obsid" should be an integer or None, but got %r', obsid)

        return cls(name, type, obsid, source, size, md5, datetime.datetime.fromtimestamp(ctime_unix))

    def make_generic_event(self, type, **kwargs):
        """Create a new FileEvent record relating to this file. The new event is not
        added or committed to the database.

        """
        return FileEvent(self.name, type, kwargs)

    def make_instance_creation_event(self, instance, store):
        return self.make_generic_event('create_instance',
                                       store_name=store.name,
                                       parent_dirs=instance.parent_dirs)

    def make_instance_deletion_event(self, instance, store):
        return self.make_generic_event('delete_instance',
                                       store_name=store.name,
                                       parent_dirs=instance.parent_dirs)

    def make_copy_launched_event(self, connection_name, remote_store_path):
        return self.make_generic_event('launch_copy',
                                       connection_name=connection_name,
                                       remote_store_path=remote_store_path)

    def make_copy_finished_event(self, connection_name, remote_store_path,
                                 error_code, error_message, duration=None,
                                 average_rate=None):
        extras = {}

        if duration is not None:
            extras['duration'] = duration  # seconds
        if average_rate is not None:
            extras['average_rate'] = average_rate  # kilobytes/sec

        return self.make_generic_event('copy_finished',
                                       connection_name=connection_name,
                                       remote_store_path=remote_store_path,
                                       error_code=error_code,
                                       error_message=error_message,
                                       **extras)


class DeletionPolicy (object):
    """A simple enumeration of symbolic constants for the "deletion_policy"
    column in the FileInstance table.

    """
    DISALLOWED = 0
    ALLOWED = 1

    def __init__(self): assert False, 'instantiation of enum not allowed'

    @classmethod
    def parse_safe(cls, text):
        if text == 'disallowed':
            return cls.DISALLOWED
        if text == 'allowed':
            return cls.ALLOWED

        logger.warn('unrecognized deletion policy %r; using DISALLOWED', text)
        return cls.DISALLOWED

    @classmethod
    def textualize(cls, value):
        if value == cls.DISALLOWED:
            return 'disallowed'
        if value == cls.ALLOWED:
            return 'allowed'
        return '???(%r)' % (value, )


class FileInstance (db.Model):
    """A FileInstance is a copy of a File that lives on one of this Librarian's
    stores.

    Because the File record knows the key attributes of the file that we're
    instantiating (size, MD5 sum), a FileInstance record only needs to keep
    track of the location of this instance: its store, its parent directory,
    and the file name (which, because File names are unique, is a foreign key
    into the File table).

    Even though File names are unique, for organizational purposes they are
    sorted into directories when instantiated in actual stores. In current
    practice this is generally done by JD although this is not baked into the
    design.

    """
    __tablename__ = 'file_instance'

    store = db.Column(db.BigInteger, db.ForeignKey(Store.id), primary_key=True)
    parent_dirs = db.Column(db.String(128), primary_key=True)
    name = db.Column(db.String(256), db.ForeignKey(File.name), primary_key=True)
    deletion_policy = NotNull(db.Integer, default=DeletionPolicy.DISALLOWED)

    file = db.relationship('File', back_populates='instances')
    store_object = db.relationship('Store', back_populates='instances')

    name_index = db.Index('file_instance_name', name)

    def __init__(self, store_obj, parent_dirs, name, deletion_policy=DeletionPolicy.DISALLOWED):
        if '/' in name:
            raise ValueError('illegal file name "%s": names may not contain "/"' % name)

        self.store = store_obj.id
        self.parent_dirs = parent_dirs
        self.name = name
        self.deletion_policy = deletion_policy

    @property
    def store_name(self):
        return self.store_object.name

    @property
    def store_path(self):
        import os.path
        return os.path.join(self.parent_dirs, self.name)

    def full_path_on_store(self):
        import os.path
        return os.path.join(self.store_object.path_prefix, self.parent_dirs, self.name)

    def descriptive_name(self):
        return self.store_name + ':' + self.store_path

    @property
    def deletion_policy_text(self):
        return DeletionPolicy.textualize(self.deletion_policy)

    def to_dict(self):
        return dict(
            store_name=self.store_object.name,
            store_ssh_host=self.store_object.ssh_host,
            parent_dirs=self.parent_dirs,
            name=self.name,
            deletion_policy=self.deletion_policy_text,
            full_path_on_store=self.full_path_on_store()
        )


class FileEvent (db.Model):
    """A FileEvent is a something that happens to a File on this Librarian.

    Note that events are per-File, not per-FileInstance. One reason for this
    is that FileInstance records may get deleted, and we want to be able to track
    history even after that happens.

    On the other hand, FileEvents are private per Librarian. They are not
    synchronized from one Librarian to another and are not globally unique.

    The nature of a FileEvent payload is defined by its type. We suggest
    JSON-encoded text. The payload is limited to 512 bytes so there's only so
    much you can carry.

    """
    __tablename__ = 'file_event'

    id = db.Column(db.BigInteger, primary_key=True)
    name = db.Column(db.String(256), db.ForeignKey(File.name))
    time = NotNull(db.DateTime)
    type = db.Column(db.String(64))
    payload = db.Column(db.Text)
    file = db.relationship('File', back_populates='events')

    name_index = db.Index('file_event_name', name)

    def __init__(self, name, type, payload_struct):
        if '/' in name:
            raise ValueError('illegal file name "%s": names may not contain "/"' % name)

        self.name = name
        self.time = datetime.datetime.utcnow().replace(microsecond=0)
        self.type = type
        self.payload = json.dumps(payload_struct)

    @property
    def payload_json(self):
        return json.loads(self.payload)


# RPC endpoints

@app.route('/api/create_file_event', methods=['GET', 'POST'])
@json_api
def create_file_event(args, sourcename=None):
    """Create a FileEvent record for a File.

    We enforce basically no structure on the event data.

    """
    file_name = required_arg(args, str, 'file_name')
    type = required_arg(args, str, 'type')
    payload = required_arg(args, dict, 'payload')

    file = File.query.get(file_name)
    if file is None:
        raise ServerError('no known file "%s"', file_name)

    event = file.make_generic_event(type, **payload)
    db.session.add(event)

    try:
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        app.log_exception(sys.exc_info())
        raise ServerError('failed to add event to database -- see server logs for details')

    return {}


@app.route('/api/locate_file_instance', methods=['GET', 'POST'])
@json_api
def locate_file_instance(args, sourcename=None):
    """Tell the caller where to find an instance of the named file.

    """
    file_name = required_arg(args, str, 'file_name')

    file = File.query.get(file_name)
    if file is None:
        raise ServerError('no known file "%s"', file_name)

    for inst in file.instances:
        return {
            'full_path_on_store': inst.full_path_on_store(),
            'store_name': inst.store_name,
            'store_path': inst.store_path,
            'store_ssh_host': inst.store_object.ssh_host,
        }

    raise ServerError('no instances of file "%s" on this librarian', file_name)


@app.route('/api/set_one_file_deletion_policy', methods=['GET', 'POST'])
@json_api
def set_one_file_deletion_policy(args, sourcename=None):
    """Set the deletion policy of one instance of a file.

    The "one instance" restriction is just a bit of a sanity-check to throw up
    barriers against deleting all instances of a file if more than one
    instance actually exists.

    If the optional 'restrict_to_store' argument is supplied, only instances
    on the specified store will be modified. This is useful when clearing out
    a store for deactivation (see also the "offload" functionality). Note that
    the "one instance" limit still applies.

    """
    file_name = required_arg(args, str, 'file_name')
    deletion_policy = required_arg(args, str, 'deletion_policy')
    restrict_to_store = optional_arg(args, str, 'restrict_to_store')
    if restrict_to_store is not None:
        from .store import Store
        restrict_to_store = Store.get_by_name(restrict_to_store)  # ServerError if lookup fails

    file = File.query.get(file_name)
    if file is None:
        raise ServerError('no known file "%s"', file_name)

    deletion_policy = DeletionPolicy.parse_safe(deletion_policy)

    for inst in file.instances:
        # We could do this filter in SQL but it's easier to just do it this way;
        # you can't call filter() on `file.instances`.
        if restrict_to_store is not None and inst.store != restrict_to_store.id:
            continue

        inst.deletion_policy = deletion_policy
        break  # just one!
    else:
        raise ServerError('no instances of file "%s" on this librarian', file_name)

    db.session.add(file.make_generic_event('instance_deletion_policy_changed',
                                           store_name=inst.store_object.name,
                                           parent_dirs=inst.parent_dirs,
                                           new_policy=deletion_policy))

    try:
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        app.log_exception(sys.exc_info())
        raise ServerError('failed to commit changes to the database')

    return {}


@app.route('/api/delete_file_instances', methods=['GET', 'POST'])
@json_api
def delete_file_instances(args, sourcename=None):
    """DANGER ZONE! Delete instances of the named file on all stores!

    See File.delete_instances for a description of the safety interlocks.

    """
    file_name = required_arg(args, str, 'file_name')
    mode = optional_arg(args, str, 'mode', 'standard')
    restrict_to_store = optional_arg(args, str, 'restrict_to_store')
    if restrict_to_store is not None:
        from .store import Store
        restrict_to_store = Store.get_by_name(restrict_to_store)  # ServerError if lookup fails

    file = File.query.get(file_name)
    if file is None:
        raise ServerError('no known file "%s"', file_name)

    return file.delete_instances(mode=mode, restrict_to_store=restrict_to_store)


@app.route('/api/delete_file_instances_matching_query', methods=['GET', 'POST'])
@json_api
def delete_file_instances_matching_query(args, sourcename=None):
    """DANGER ZONE! Delete instances of lots of files on the store!

    See File.delete_instances for a description of the safety interlocks.

    """
    query = required_arg(args, str, 'query')
    mode = optional_arg(args, str, 'mode', 'standard')
    restrict_to_store = optional_arg(args, str, 'restrict_to_store')
    if restrict_to_store is not None:
        from .store import Store
        restrict_to_store = Store.get_by_name(restrict_to_store)  # ServerError if lookup fails

    from .search import compile_search
    query = compile_search(query, query_type='files')
    stats = {}

    for file in query:
        stats[file.name] = file.delete_instances(mode=mode, restrict_to_store=restrict_to_store)

    return {
        'stats': stats,
    }


# Web user interface

@app.route('/files/<string:name>')
@login_required
def specific_file(name):
    file = File.query.get(name)
    if file is None:
        flash('No such file "%s" known' % name)
        return redirect(url_for('index'))

    instances = list(FileInstance.query.filter(FileInstance.name == name))
    events = sorted(file.events, key=lambda e: e.time, reverse=True)

    return render_template(
        'file-individual.html',
        title='%s File %s' % (file.type, file.name),
        file=file,
        instances=instances,
        events=events,
    )
