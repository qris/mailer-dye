import os
from os import path
from datetime import datetime
import getpass
import re
import time

from fabric.context_managers import cd, hide, settings
from fabric.operations import require, prompt, get, run, sudo, local
from fabric.state import env
from fabric.contrib import files
from fabric import utils


def _setup_paths(project_settings):
    # first merge in variables from project_settings - but ignore __doc__ etc
    user_settings = [x for x in vars(project_settings).keys() if not x.startswith('__')]
    for setting in user_settings:
        env[setting] = vars(project_settings)[setting]

    # allow for project_settings having set up some of these differently
    env.setdefault('verbose', False)
    env.setdefault('use_sudo', True)
    env.setdefault('cvs_rsh', 'CVS_RSH="ssh"')
    env.setdefault('default_branch', {'production': 'master', 'staging': 'master'})
    env.setdefault('server_project_home',
                   path.join(env.server_home, env.project_name))
    # TODO: change dev -> current
    env.setdefault('vcs_root_dir', path.join(env.server_project_home, 'dev'))
    env.setdefault('prev_root', path.join(env.server_project_home, 'previous'))
    env.setdefault('next_dir', path.join(env.server_project_home, 'next'))
    env.setdefault('dump_dir', path.join(env.server_project_home, 'dbdumps'))
    env.setdefault('deploy_dir', path.join(env.vcs_root_dir, 'deploy'))
    env.setdefault('settings', '%(project_name)s.settings' % env)

    if env.project_type == "django":
        env.setdefault('relative_django_dir', env.project_name)
        env.setdefault('relative_django_settings_dir', env['relative_django_dir'])
        env.setdefault('relative_ve_dir', path.join(env['relative_django_dir'], '.ve'))

        # now create the absolute paths of everything else
        env.setdefault('django_dir',
                    path.join(env['vcs_root_dir'], env['relative_django_dir']))
        env.setdefault('django_settings_dir',
                    path.join(env['vcs_root_dir'], env['relative_django_settings_dir']))
        env.setdefault('ve_dir',
                    path.join(env['vcs_root_dir'], env['relative_ve_dir']))
        env.setdefault('manage_py', path.join(env['django_dir'], 'manage.py'))

    # local_tasks_bin is the local copy of tasks.py
    # this should be the copy from where ever fab.py is being run from ...
    if 'DEPLOYDIR' in os.environ:
        env.setdefault('local_tasks_bin',
            path.join(os.environ['DEPLOYDIR'], 'tasks.py'))
    else:
        env.setdefault('local_tasks_bin',
            path.join(path.dirname(__file__), 'tasks.py'))

    # valid environments - used for require statements in fablib
    env.valid_envs = env.host_list.keys()


def _linux_type():
    if 'linux_type' not in env:
        # work out if we're based on redhat or centos
        # TODO: look up stackoverflow question about this.
        if files.exists('/etc/redhat-release'):
            env.linux_type = 'redhat'
        elif files.exists('/etc/debian_version'):
            env.linux_type = 'debian'
        else:
            # TODO: should we print a warning here?
            utils.abort("could not determine linux type of server we're deploying to")
    return env.linux_type


def _get_python():
    if 'python_bin' not in env:
        python26 = path.join('/', 'usr', 'bin', 'python2.6')
        if files.exists(python26):
            env.python_bin = python26
        else:
            env.python_bin = path.join('/', 'usr', 'bin', 'python')
    return env.python_bin


def _get_tasks_bin():
    if 'tasks_bin' not in env:
        env.tasks_bin = path.join(env.deploy_dir, 'tasks.py')
    return env.tasks_bin


def _tasks(tasks_args, verbose=False):
    tasks_cmd = _get_tasks_bin()
    if env.verbose or verbose:
        tasks_cmd += ' -v'
    sudo_or_run(tasks_cmd + ' ' + tasks_args)


def _get_svn_user_and_pass():
    if 'svnuser' not in env or len(env.svnuser) == 0:
        # prompt user for username
        prompt('Enter SVN username:', 'svnuser')
    if 'svnpass' not in env or len(env.svnpass) == 0:
        # prompt user for password
        env.svnpass = getpass.getpass('Enter SVN password:')


def verbose(verbose=True):
    """Set verbose output"""
    env.verbose = verbose


def deploy_clean(revision=None):
    """ delete the entire install and do a clean install """
    if env.environment == 'production':
        utils.abort('do not delete the production environment!!!')
    require('server_project_home', provided_by=env.valid_envs)
    # TODO: dump before cleaning database?
    with settings(warn_only=True):
        webserver_cmd('stop')
    clean_db()
    clean_files()
    deploy(revision)


def clean_files():
    sudo_or_run('rm -rf %s' % env.server_project_home)


def _create_dir_if_not_exists(path):
    if not files.exists(path):
        sudo_or_run('mkdir -p %s' % path)


def deploy(revision=None, keep=None):
    """ update remote host environment (virtualenv, deploy, update)

    It takes two arguments:

    * revision is the VCS revision ID to checkout (if not specified then
      the latest will be checked out)
    * keep is the number of old versions to keep around for rollback (default
      5)"""
    require('server_project_home', provided_by=env.valid_envs)
    check_for_local_changes()

    _create_dir_if_not_exists(env.server_project_home)

    # TODO: check if our live site is in <sitename>/dev/ - if so
    # move it to <sitename>/current/ and make a link called dev/ to
    # the current/ directory
    # TODO: if dev/ is found to be a link, ask the user if the apache config
    # has been updated to point at current/ - and if so then delete dev/
    # _migrate_from_dev_to_current()
    create_copy_for_next()
    checkout_or_update(in_next=True, revision=revision)
    # remove any old pyc files - essential if the .py file has been removed
    if env.project_type == "django":
        rm_pyc_files(path.join(env.next_dir, env.relative_django_dir))
    # create the deploy virtualenv if we use it
    create_deploy_virtualenv(in_next=True)

    # we only have to disable this site after creating the rollback copy
    # (do this so that apache carries on serving other sites on this server
    # and the maintenance page for this vhost)
    downtime_start = datetime.now()
    link_webserver_conf(maintenance=True)
    with settings(warn_only=True):
        webserver_cmd('reload')
    next_to_current_to_rollback()

    # Use tasks.py deploy:env to actually do the deployment, including
    # creating the virtualenv if it thinks it necessary, ignoring
    # env.use_virtualenv as tasks.py knows nothing about it.
    _tasks('deploy:' + env.environment)

    # bring this vhost back in, reload the webserver and touch the WSGI
    # handler (which reloads the wsgi app)
    link_webserver_conf()
    webserver_cmd('reload')
    downtime_end = datetime.now()
    touch_wsgi()

    delete_old_rollback_versions(keep)
    if env.environment == 'production':
        setup_db_dumps()

    _report_downtime(downtime_start, downtime_end)


def _report_downtime(downtime_start, downtime_end):
    downtime = downtime_end - downtime_start
    utils.puts("Downtime lasted for %.1f seconds" % downtime.total_seconds())
    utils.puts("(Downtime started at %s and finished at %s)" %
               (downtime_start, downtime_end))


def set_up_celery_daemon():
    require('vcs_root_dir', 'project_name', provided_by=env)
    for command in ('celerybeat', 'celeryd'):
        command_project = command + '_' + env.project_name
        celery_run_script_location = path.join(env['vcs_root_dir'],
                                               'celery', 'init', command)
        celery_run_script = path.join('/etc', 'init.d', command_project)
        celery_configuration_location = path.join(env['vcs_root_dir'],
                                                  'celery', 'config', command)
        celery_configuration_destination = path.join('/etc', 'default',
                                                     command_project)

        sudo_or_run(" ".join(['cp', celery_run_script_location,
                    celery_run_script]))
        sudo_or_run(" ".join(['chmod', '+x', celery_run_script]))

        sudo_or_run(" ".join(['cp', celery_configuration_location,
                    celery_configuration_destination]))
        sudo_or_run('/etc/init.d/%s restart' % command_project)


def clean_old_celery():
    """As the scripts have moved location you might need to get rid of old
    versions of celery."""
    require('vcs_root_dir', provided_by=env)
    for command in ('celerybeat', 'celeryd'):
        celery_run_script = path.join('/etc', 'init.d', command)
        if files.exists(celery_run_script):
            sudo_or_run('/etc/init.d/%s stop' % command)
            sudo_or_run('rm %s' % celery_run_script)

        celery_configuration_destination = path.join('/etc', 'default', command)
        if files.exists(celery_configuration_destination):
            sudo_or_run('rm %s' % celery_configuration_destination)


def create_copy_for_next():
    """Copy the current version to "next" so that we can do stuff like
    the VCS update and virtualenv update without taking the site offline"""
    # TODO: check if next directory already exists
    # if it does maybe there was an aborted deploy, or maybe someone else is
    # deploying.  Either way, stop and ask the user what to do.
    if files.exists(env.next_dir):
        utils.warn('The "next" directory already exists.  Maybe a previous '
                   'deploy failed, or maybe another deploy is in progress.')
        continue_anyway = prompt('Would you like to continue anyway '
                                 '(and delete the current next dir)? [no/yes]',
                default='no', validate='^no|yes$')
        if continue_anyway.lower() != 'yes':
            utils.abort("Aborting deploy - try again when you're certain what to do.")
        sudo_or_run('rm -rf %s' % env.next_dir)

    # if this is the initial deploy, the vcs_root_dir won't exist yet. In that
    # case, don't create it (otherwise the checkout code will get confused).
    if files.exists(env.vcs_root_dir):
        # cp -a - amongst other things this preserves links and timestamps
        # so the compare that bootstrap.py does to see if the virtualenv
        # needs an update should still work.
        sudo_or_run('cp -a %s %s' % (env.vcs_root_dir, env.next_dir))


def next_to_current_to_rollback():
    """Move the current version to the previous directory (so we can roll back
    to it, move the next version to the current version (so it will be used) and
    do a db dump in the rollback directory."""
    # create directory for it
    # if this is the initial deploy, the vcs_root_dir won't exist yet.  In that
    # case just skip the rollback version.
    if files.exists(env.vcs_root_dir):
        _create_dir_if_not_exists(env.prev_root)
        prev_dir = path.join(env.prev_root, time.strftime("%Y-%m-%d_%H-%M-%S"))
        sudo_or_run('mv %s %s' % (env.vcs_root_dir, prev_dir))
        _dump_db_in_previous_directory(prev_dir)
    sudo_or_run('mv %s %s' % (env.next_dir, env.vcs_root_dir))


def create_copy_for_rollback():
    """Move the current version to the previous directory (so we can roll back
    to it, move the next version to the current version (so it will be used) and
    do a db dump in the rollback directory."""
    # create directory for it
    prev_dir = path.join(env.prev_root, time.strftime("%Y-%m-%d_%H-%M-%S"))
    _create_dir_if_not_exists(prev_dir)
    # cp -a
    sudo_or_run('cp %s %s' % (env.vcs_root_dir, prev_dir))
    _dump_db_in_previous_directory(prev_dir)


def _dump_db_in_previous_directory(prev_dir):
    require('django_settings_dir', provided_by=env.valid_envs)
    if (env.project_type == 'django' and
            files.exists(path.join(env.django_settings_dir, 'local_settings.py'))):
        # dump database (provided local_settings has been set up properly)
        with cd(prev_dir):
            # just in case there is some other reason why the dump fails
            with settings(warn_only=True):
                _tasks('dump_db')


def delete_old_rollback_versions(keep=None):
    """Delete old rollback directories, keeping the last "keep" (default 5)"."""
    require('prev_root', provided_by=env.valid_envs)
    # the -1 argument ensures one directory per line
    prev_versions = run('ls -1 ' + env.prev_root).split('\n')
    if keep is None:
        if 'versions_to_keep' in env:
            keep = env.versions_to_keep
        else:
            keep = 5
    else:
        keep = int(keep)
    if keep == 0:
        return
    versions_to_keep = -1 * int(keep)
    prev_versions_to_delete = prev_versions[:versions_to_keep]
    for version_to_delete in prev_versions_to_delete:
        sudo_or_run('rm -rf ' + path.join(
            env.prev_root, version_to_delete.strip()))


def list_previous():
    """List the previous versions available to rollback to."""
    # could also determine the VCS revision number
    require('prev_root', provided_by=env.valid_envs)
    run('ls ' + env.prev_root)


def rollback(version='last', migrate=False, restore_db=False):
    """Redeploy one of the old versions.

    Arguments are 'version', 'migrate' and 'restore_db':

    * if version is 'last' (the default) then the most recent version will be
      restored. Otherwise specify by timestamp - use list_previous to get a list
      of available versions.
    * if restore_db is True, then the database will be restored as well as the
      code. The default is False.
    * if migrate is True, then fabric will attempt to work out the new and old
      migration status and run the migrations to match the database versions.
      The default is False

    Note that migrate and restore_db cannot both be True."""
    require('prev_root', 'vcs_root_dir', provided_by=env.valid_envs)
    if migrate and restore_db:
        utils.abort('rollback cannot do both migrate and restore_db')
    if migrate:
        utils.abort("rollback: haven't worked out how to do migrate yet ...")

    if version == 'last':
        # get the latest directory from prev_dir
        # list directories in env.prev_root, use last one
        version = run('ls ' + env.prev_root).split('\n')[-1]
    # check version specified exists
    rollback_dir = path.join(env.prev_root, version)
    if not files.exists(rollback_dir):
        utils.abort("Cannot rollback to version %s, it does not exist, use list_previous to see versions available" % version)

    webserver_cmd("stop")
    # first copy this version out of the way
    create_copy_for_rollback()
    if migrate:
        # run the south migrations back to the old version
        # but how to work out what the old version is??
        pass
    if restore_db:
        # feed the dump file into mysql command
        with cd(rollback_dir):
            _tasks('load_dbdump')
    # delete everything - don't want stray files left over
    sudo_or_run('rm -rf %s' % env.vcs_root_dir)
    # cp -a from rollback_dir to vcs_root_dir
    sudo_or_run('cp -a %s %s' % (rollback_dir, env.vcs_root_dir))
    webserver_cmd("start")


def local_test():
    """ run the django tests on the local machine """
    require('project_name')
    with cd(path.join("..", env.project_name)):
        local("python " + env.test_cmd, capture=False)


def remote_test():
    """ run the django tests remotely - staging only """
    require('django_dir', provided_by=env.valid_envs)
    if env.environment == 'production':
        utils.abort('do not run tests on the production environment')
    with cd(env.django_dir):
        sudo_or_run(_get_python() + env.test_cmd)


def version():
    """ return the deployed VCS revision and commit comments"""
    require('server_project_home', 'repo_type', 'vcs_root_dir', 'repository',
        provided_by=env.valid_envs)
    if env.repo_type == "git":
        with cd(env.vcs_root_dir):
            sudo_or_run('git log | head -5')
    elif env.repo_type == "svn":
        _get_svn_user_and_pass()
        with cd(env.vcs_root_dir):
            with hide('running'):
                cmd = 'svn log --non-interactive --username %s --password %s | head -4' % (env.svnuser, env.svnpass)
                sudo_or_run(cmd)
    else:
        utils.abort('Unsupported repo type: %s' % (env.repo_type))


def _check_git_branch():
    env.revision = None
    with cd(env.vcs_root_dir):
        with settings(warn_only=True):
            # get branch information
            server_branch = sudo_or_run('git rev-parse --abbrev-ref HEAD')
            server_commit = sudo_or_run('git rev-parse HEAD')
            local_branch = local('git rev-parse --abbrev-ref HEAD', capture=True)
            default_branch = env.default_branch.get(env.environment, 'master')
            git_branch_r = sudo_or_run('git branch --color=never -r')
            git_branch_r = git_branch_r.split('\n')
            branches = [b.split('/')[-1].strip() for b in git_branch_r if 'HEAD' not in b]

        # if all branches are the same, just stick to this branch
        if server_branch == local_branch == default_branch:
            env.revision = server_branch
        else:
            if server_branch == 'HEAD':
                # not on a branch - just print a warning
                print 'The server git repository is not on a branch'

            print 'Branch mismatch found:'
            print '* %s is the default branch for this server' % default_branch
            if server_branch == 'HEAD':
                print '* %s is the commit checked out on the server.' % server_commit
            else:
                print '* %s is the branch currently checked out on the server' % server_branch
            print '* %s is the current branch of your local git repo' % local_branch
            print ''
            print 'Available branches are:'
            for branch in branches:
                print '* %s' % branch
            print ''
            escaped_branches = [re.escape(b) for b in branches]
            validate_branch = '^' + '|'.join(escaped_branches) + '$'

            env.revision = prompt('Which branch would you like to use on the server? (or hit Ctrl-C to exit)',
                    default=default_branch, validate=validate_branch)


def check_for_local_changes():
    """ check if there are local changes on the remote server """
    require('repo_type', 'vcs_root_dir', provided_by=env.valid_envs)
    status_cmd = {
        'svn': 'svn status --quiet',
        'git': 'git status --short',
        'cvs': '#not worked out yet'
    }
    if env.repo_type == 'cvs':
        print "TODO: write CVS status command"
        return
    if files.exists(path.join(env.vcs_root_dir, "." + env.repo_type)):
        with cd(env.vcs_root_dir):
            status = sudo_or_run(status_cmd[env.repo_type])
            if status:
                print 'Found local changes on %s server' % env.environment
                print status
                cont = prompt('Would you like to continue with deployment? (yes/no)',
                        default='no', validate=r'^yes|no$')
                if cont == 'no':
                    utils.abort('Aborting deployment')
        if env.repo_type == 'git':
            _check_git_branch()


def checkout_or_update(in_next=False, revision=None):
    """ checkout or update the project from version control.

    This command works with svn, git and cvs repositories.

    You can also specify a revision to checkout, as an argument."""
    require('server_project_home', 'repo_type', 'vcs_root_dir', 'repository',
        provided_by=env.valid_envs)
    checkout_fn = {
        'cvs': _checkout_or_update_cvs,
        'svn': _checkout_or_update_svn,
        'git': _checkout_or_update_git,
    }
    if in_next:
        vcs_root_dir = env.next_dir
    else:
        vcs_root_dir = env.vcs_root_dir
    if env.repo_type.lower() in checkout_fn:
        checkout_fn[env.repo_type](vcs_root_dir, revision)
    else:
        utils.abort('Unsupported VCS: %s' % env.repo_type.lower())


def _checkout_or_update_svn(vcs_root_dir, revision=None):
    # function to ask for svnuser and svnpass
    _get_svn_user_and_pass()
    # if the .svn directory exists, do an update, otherwise do
    # a checkout
    cmd = 'svn %s --non-interactive --no-auth-cache --username %s --password %s'
    if files.exists(path.join(vcs_root_dir, ".svn")):
        cmd = cmd % ('update', env.svnuser, env.svnpass)
        if revision:
            cmd += " --revision " + revision
        with cd(vcs_root_dir):
            with hide('running'):
                sudo_or_run(cmd)
    else:
        cmd = cmd + " %s %s"
        cmd = cmd % ('checkout', env.svnuser, env.svnpass, env.repository, vcs_root_dir)
        if revision:
            cmd += "@" + revision
        with cd(env.server_project_home):
            with hide('running'):
                sudo_or_run(cmd)


def _checkout_or_update_git(vcs_root_dir, revision=None):
    # if the .git directory exists, do an update, otherwise do
    # a clone
    if files.exists(path.join(vcs_root_dir, ".git")):
        with cd(vcs_root_dir):
            sudo_or_run('git remote rm origin')
            sudo_or_run('git remote add origin %s' % env.repository)
            # fetch now, merge later (if on branch)
            sudo_or_run('git fetch origin')

        if revision is None:
            revision = env.revision

        with cd(vcs_root_dir):
            stash_result = sudo_or_run('git stash')
            sudo_or_run('git checkout %s' % revision)
            # check if revision is a branch, and do a merge if it is
            with settings(warn_only=True):
                rev_is_branch = sudo_or_run('git branch -r | grep %s' % revision)
            # use old fabric style here to support Ubuntu 10.04
            if not rev_is_branch.failed:
                sudo_or_run('git merge origin/%s' % revision)
            # if we did a stash, now undo it
            if not stash_result.startswith("No local changes"):
                sudo_or_run('git stash pop')
    else:
        with cd(env.server_project_home):
            default_branch = env.default_branch.get(env.environment, 'master')
            sudo_or_run('git clone -b %s %s %s' %
                    (default_branch, env.repository, vcs_root_dir))

    if files.exists(path.join(vcs_root_dir, ".gitmodules")):
        with cd(vcs_root_dir):
            sudo_or_run('git submodule update --init')


def _checkout_or_update_cvs(vcs_root_dir, revision=None):
    if files.exists(vcs_root_dir):
        with cd(vcs_root_dir):
            sudo_or_run('CVS_RSH="ssh" cvs update -d -P')
    else:
        if 'cvs_user' in env:
            user_spec = env.cvs_user + "@"
        else:
            user_spec = ""

        with cd(env.server_project_home):
            cvs_options = '-d:%s:%s%s:%s' % (env.cvs_connection_type,
                                             user_spec,
                                             env.repository,
                                             env.repo_path)
            command_options = '-d %s' % vcs_root_dir

            if revision is not None:
                command_options += ' -r ' + revision

            sudo_or_run('%s cvs %s checkout %s %s' % (env.cvs_rsh, cvs_options,
                                                      command_options,
                                                      env.cvs_project))


def sudo_or_run(command):
    if env.use_sudo:
        return sudo(command)
    else:
        return run(command)


def create_deploy_virtualenv(in_next=False):
    """ if using new style dye stuff, create the virtualenv to hold dye """
    require('deploy_dir', provided_by=env.valid_envs)
    if in_next:
        # TODO: use relative_deploy_dir
        bootstrap_path = path.join(env.next_dir, 'deploy', 'bootstrap.py')
    else:
        bootstrap_path = path.join(env.deploy_dir, 'bootstrap.py')
    sudo_or_run('%s %s --full-rebuild --quiet' %
                (_get_python(), bootstrap_path))


def update_requirements():
    """ update external dependencies on remote host """
    _tasks('update_ve')


def collect_static_files():
    """ coolect static files in the 'static' directory """
    sudo(_get_tasks_bin() + ' collect_static')


def clean_db(revision=None):
    """ delete the entire database """
    if env.environment == 'production':
        utils.abort('do not delete the production database!!!')
    _tasks("clean_db")


def get_remote_dump(filename='/tmp/db_dump.sql', local_filename='./db_dump.sql',
        rsync=True):
    """ do a remote database dump and copy it to the local filesystem """
    # future enhancement, do a mysqldump --skip-extended-insert (one insert
    # per line) and then do rsync rather than get() - less data transferred on
    # however rsync might need ssh keys etc
    require('user', 'host', provided_by=env.valid_envs)
    if rsync:
        _tasks('dump_db:' + filename + ',for_rsync=true')
        local("rsync -vz -e 'ssh -p %s' %s@%s:%s %s" % (env.port,
            env.user, env.host, filename, local_filename))
    else:
        _tasks('dump_db:' + filename)
        get(filename, local_path=local_filename)
    sudo_or_run('rm ' + filename)


def get_remote_dump_and_load(filename='/tmp/db_dump.sql',
        local_filename='./db_dump.sql', keep_dump=True, rsync=True):
    """ do a remote database dump, copy it to the local filesystem and then
    load it into the local database """
    get_remote_dump(filename=filename, local_filename=local_filename, rsync=rsync)
    local(env.local_tasks_bin + ' restore_db:' + local_filename)
    if not keep_dump:
        local('rm ' + local_filename)


def update_db(force_use_migrations=False):
    """ create and/or update the database, do migrations etc """
    _tasks('update_db:force_use_migrations=%s' % force_use_migrations)


def setup_db_dumps():
    """ set up mysql database dumps """
    require('dump_dir', provided_by=env.valid_envs)
    _tasks('setup_db_dumps:' + env.dump_dir)


def touch_wsgi():
    """ touch wsgi file to trigger reload """
    require('vcs_root_dir', provided_by=env.valid_envs)
    wsgi_dir = path.join(env.vcs_root_dir, 'wsgi')
    sudo_or_run('touch ' + path.join(wsgi_dir, 'wsgi_handler.py'))


def rm_pyc_files(py_dir=None):
    """Remove all the old pyc files to prevent stale files being used"""
    require('django_dir', provided_by=env.valid_envs)
    if py_dir is None:
        py_dir = env.django_dir
    with settings(warn_only=True):
        with cd(py_dir):
            sudo_or_run('find . -name \*.pyc | xargs rm')


def _delete_file(path):
    if files.exists(path):
        sudo_or_run('rm %s' % path)


def _link_files(source_file, target_path):
    if not files.exists(target_path):
        sudo_or_run('ln -s %s %s' % (source_file, target_path))


def link_webserver_conf(maintenance=False):
    """link the webserver conf file"""
    require('vcs_root_dir', provided_by=env.valid_envs)
    if env.webserver is None:
        return
    vcs_config_stub = path.join(env.vcs_root_dir, env.webserver, env.environment)
    vcs_config_live = vcs_config_stub + '.conf'
    vcs_config_maintenance = vcs_config_stub + '-maintenance.conf'
    webserver_conf = _webserver_conf_path()

    if maintenance:
        _delete_file(webserver_conf)
        if not files.exists(vcs_config_maintenance):
            return
        _link_files(vcs_config_maintenance, webserver_conf)
    else:
        if not files.exists(vcs_config_live):
            utils.abort('No %s conf file found - expected %s' %
                    (env.webserver, vcs_config_live))
        _delete_file(webserver_conf)
        _link_files(vcs_config_live, webserver_conf)

    # debian has sites-available/sites-enabled split with links
    if _linux_type() == 'debian':
        webserver_conf_enabled = webserver_conf.replace('available', 'enabled')
        sudo_or_run('ln -s %s %s' % (webserver_conf, webserver_conf_enabled))
    webserver_configtest()


def _webserver_conf_path():
    webserver_conf_dir = {
        'apache_redhat': '/etc/httpd/conf.d',
        'apache_debian': '/etc/apache2/sites-available',
    }
    key = env.webserver + '_' + _linux_type()
    if key in webserver_conf_dir:
        return path.join(webserver_conf_dir[key],
            '%s_%s.conf' % (env.project_name, env.environment))
    else:
        utils.abort('webserver %s is not supported (linux type %s)' %
                (env.webserver, _linux_type()))


def webserver_configtest():
    """ test webserver configuration """
    tests = {
        'apache_redhat': '/usr/sbin/httpd -S',
        'apache_debian': '/usr/sbin/apache2ctl -S',
    }
    if env.webserver:
        key = env.webserver + '_' + _linux_type()
        if key in tests:
            sudo(tests[key])
        else:
            utils.abort('webserver %s is not supported (linux type %s)' %
                    (env.webserver, _linux_type()))


def webserver_reload():
    """ reload webserver on remote host """
    webserver_cmd('reload')


def webserver_restart():
    """ restart webserver on remote host """
    webserver_cmd('restart')


def webserver_cmd(cmd):
    """ run cmd against webserver init.d script """
    cmd_strings = {
        'apache_redhat': '/etc/init.d/httpd',
        'apache_debian': '/etc/init.d/apache2',
    }
    if env.webserver:
        key = env.webserver + '_' + _linux_type()
        if key in cmd_strings:
            sudo(cmd_strings[key] + ' ' + cmd)
        else:
            utils.abort('webserver %s is not supported' % env.webserver)
