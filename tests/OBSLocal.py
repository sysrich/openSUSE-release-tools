import os
from lxml import etree as ET
import subprocess
import unittest
import logging
import os.path
import random
import string
import sys
import traceback

from osc import conf
from osc import oscerr
import osc.conf
import osc.core
from osc.core import get_request
from osc.core import http_GET
from osc.core import makeurl

from osclib.cache import Cache
from osclib.cache_manager import CacheManager
from osclib.conf import Config
from osclib.freeze_command import FreezeCommand
from osclib.stagingapi import StagingAPI
from osclib.core import attribute_value_save
from osclib.core import request_state_change
from osclib.memoize import memoize_session_reset

from urllib.error import HTTPError, URLError

# pointing to other docker container
APIURL = 'http://api:3000'
PROJECT = 'openSUSE:Factory'

OSCRC = '/tmp/.oscrc-test'
OSCCOOKIEJAR = '/tmp/.osc_cookiejar-test'

class TestCase(unittest.TestCase):
    script = None
    script_apiurl = True
    script_debug = True
    script_debug_osc = True

    def setUp(self):
        if os.path.exists(OSCCOOKIEJAR):
            # Avoid stale cookiejar since local OBS may be completely reset.
            os.remove(OSCCOOKIEJAR)

        self.users = []
        self.osc_user('Admin')
        self.apiurl = conf.config['apiurl']
        self.assertOBS()

    def tearDown(self):
        # Ensure admin user so that tearDown cleanup succeeds.
        self.osc_user('Admin')

    def assertOBS(self):
        url = makeurl(self.apiurl, ['about'])
        root = ET.parse(http_GET(url)).getroot()
        self.assertEqual(root.tag, 'about')

    @staticmethod
    def oscrc(userid):
        with open(OSCRC, 'w+') as f:
            f.write('\n'.join([
                '[general]',
                'apiurl = http://api:3000',
                'cookiejar = {}'.format(OSCCOOKIEJAR),
                '[http://api:3000]',
                'user = {}'.format(userid),
                'pass = opensuse',
                'email = {}@example.com'.format(userid),
                '',
            ]))

    def osc_user(self, userid):
        print(f'setting osc user to {userid}')
        self.users.append(userid)
        self.oscrc(userid)

        # Rather than modify userid and email, just re-parse entire config and
        # reset authentication by clearing opener to avoid edge-cases.
        self.oscParse()

    def osc_user_pop(self):
        self.users.pop()
        self.osc_user(self.users.pop())

    def oscParse(self):
        # Otherwise, will stick to first user for a given apiurl.
        conf._build_opener.last_opener = (None, None)

        # Otherwise, will not re-parse same config file.
        if 'cp' in conf.get_configParser.__dict__:
            del conf.get_configParser.cp

        conf.get_config(override_conffile=OSCRC,
                        override_no_keyring=True,
                        override_no_gnome_keyring=True)
        os.environ['OSC_CONFIG'] = OSCRC
        os.environ['OSRT_DISABLE_CACHE'] = 'true'

    def execute_script(self, args):
        if self.script:
            args.insert(0, self.script)
        if self.script_debug:
            args.insert(1, '--debug')
        if self.script_debug_osc:
            args.insert(1, '--osc-debug')
        args.insert(0, '-p')
        args.insert(0, 'run')
        args.insert(0, 'coverage')

        self.execute(args)

    def execute_osc(self, args):
        # The wrapper allows this to work properly when osc installed via pip.
        args.insert(0, 'osc-wrapper.py')
        self.execute(args)

    def execute(self, args):
        print('$ ' + ' '.join(args)) # Print command for debugging.
        try:
            env = os.environ
            env['OSC_CONFIG'] = OSCRC
            self.output = subprocess.check_output(args, stderr=subprocess.STDOUT, text=True, env=env)
        except subprocess.CalledProcessError as e:
            print(e.output)
            raise e
        print(self.output) # For debugging assertion failures.

    def assertOutput(self, text):
        self.assertTrue(text in self.output, '[MISSING] ' + text)

    def assertReview(self, rid, **kwargs):
        request = get_request(self.apiurl, rid)
        for review in request.reviews:
            for key, value in kwargs.items():
                if hasattr(review, key) and getattr(review, key) == value[0]:
                    self.assertEqual(review.state, value[1], '{}={} not {}'.format(key, value[0], value[1]))
                    return review

        self.fail('{} not found'.format(kwargs))

    def assertReviewBot(self, request_id, user, before, after, comment=None):
        self.assertReview(request_id, by_user=(user, before))

        self.osc_user(user)
        self.execute_script(['id', request_id])
        self.osc_user_pop()

        review = self.assertReview(request_id, by_user=(user, after))
        if comment:
            self.assertEqual(review.comment, comment)

    def randomString(self, prefix='', length=None):
        if prefix and not prefix.endswith('_'):
            prefix += '_'
        if not length:
            length = 2
        return prefix + ''.join([random.choice(string.ascii_letters) for i in range(length)])


class StagingWorkflow(object):
    def __init__(self, project=PROJECT):
        """
        Initialize the configuration
        """
        THIS_DIR = os.path.dirname(os.path.abspath(__file__))
        oscrc = os.path.join(THIS_DIR, 'test.oscrc')

        # set to None so we return the destructor early in case of exceptions
        self.api = None
        self.apiurl = APIURL
        self.project = project
        self.projects = {}
        self.requests = []
        self.groups = []
        self.users = []
        logging.basicConfig()

        # clear cache from other tests - otherwise the VCR is replayed depending
        # on test order, which can be harmful
        memoize_session_reset()

        osc.core.conf.get_config(override_conffile=oscrc,
                                 override_no_keyring=True,
                                 override_no_gnome_keyring=True)
        os.environ['OSC_CONFIG'] = oscrc

        if os.environ.get('OSC_DEBUG'):
            osc.core.conf.config['debug'] = 1

        CacheManager.test = True
        # disable caching, the TTLs break any reproduciblity
        Cache.CACHE_DIR = None
        Cache.PATTERNS = {}
        Cache.init()
        self.setup_remote_config()
        self.load_config()
        self.api = StagingAPI(APIURL, project)

    def load_config(self, project=None):
        if project is None:
            project = self.project
        self.config = Config(APIURL, project)

    def create_attribute_type(self, namespace, name, values=None):
        meta = """
        <namespace name='{}'>
            <modifiable_by user='Admin'/>
        </namespace>""".format(namespace)
        url = osc.core.makeurl(APIURL, ['attribute', namespace, '_meta'])
        osc.core.http_PUT(url, data=meta)

        meta = "<definition name='{}' namespace='{}'><description/>".format(name, namespace)
        if values:
            meta += "<count>{}</count>".format(values)
        meta += "<modifiable_by role='maintainer'/></definition>"
        url = osc.core.makeurl(APIURL, ['attribute', namespace, name, '_meta'])
        osc.core.http_PUT(url, data=meta)

    def setup_remote_config(self):
        self.create_target()
        self.create_attribute_type('OSRT', 'Config', 1)

        config = {
            'overridden-by-local': 'remote-nope',
            'remote-only': 'remote-indeed',
        }
        self.remote_config_set(config, replace_all=True)

    def remote_config_set(self, config, replace_all=False):
        if not replace_all:
            config_existing = Config.get(self.apiurl, self.project)
            config_existing.update(config)
            config = config_existing

        config_lines = []
        for key, value in config.items():
            config_lines.append(f'{key} = {value}')

        attribute_value_save(APIURL, self.project, 'Config', '\n'.join(config_lines))

    def create_group(self, name, users=[]):

        meta = """
        <group>
          <title>{}</title>
        </group>
        """.format(name)

        if len(users):
            root = ET.fromstring(meta)
            persons = ET.SubElement(root, 'person')
            for user in users:
                ET.SubElement(persons, 'person', {'userid': user} )
            meta = ET.tostring(root)

        if not name in self.groups:
            self.groups.append(name)
        url = osc.core.makeurl(APIURL, ['group', name])
        osc.core.http_PUT(url, data=meta)

    def create_user(self, name):
        if name in self.users: return
        meta = """
        <person>
          <login>{}</login>
          <email>{}@example.com</email>
          <state>confirmed</state>
        </person>
        """.format(name, name)
        self.users.append(name)
        url = osc.core.makeurl(APIURL, ['person', name])
        osc.core.http_PUT(url, data=meta)
        url = osc.core.makeurl(APIURL, ['person', name], {'cmd': 'change_password'})
        osc.core.http_POST(url, data='opensuse')
        home_project = 'home:' + name
        self.projects[home_project] = Project(home_project, create=False)

    def create_target(self):
        if self.projects.get('target'): return
        self.create_group('factory-staging')
        self.projects['target'] = Project(name=self.project, reviewer={'groups': ['factory-staging']})
        url = osc.core.makeurl(APIURL, ['staging', self.project, 'workflow'])
        data = "<workflow managers='factory-staging'/>"
        osc.core.http_POST(url, data=data)
        # creates A and B as well
        self.projects['staging:A'] = Project(self.project + ':Staging:A', create=False)
        self.projects['staging:B'] = Project(self.project + ':Staging:B', create=False)

    def setup_rings(self):
        self.create_target()
        self.projects['ring0'] = Project(name=self.project + ':Rings:0-Bootstrap')
        self.projects['ring1'] = Project(name=self.project + ':Rings:1-MinimalX')
        target_wine = Package(name='wine', project=self.projects['target'])
        target_wine.create_commit()
        self.create_link(target_wine, self.projects['ring1'])

    def create_package(self, project, package):
        project = self.create_project(project)
        return Package(name=package, project=project)

    def create_link(self, source_package, target_project, target_package=None):
        if not target_package:
            target_package = source_package.name
        target_package = Package(name=target_package, project=target_project)
        url = self.api.makeurl(['source', target_project.name, target_package.name, '_link'])
        osc.core.http_PUT(url, data='<link project="{}" package="{}"/>'.format(source_package.project.name,
                                                                               source_package.name))
        return target_package

    def create_project(self, name, reviewer={}, maintainer={}, project_links=[]):
        if isinstance(name, Project):
            return name
        if name in self.projects:
            return self.projects[name]
        self.projects[name] = Project(name, reviewer=reviewer,
                                      maintainer=maintainer,
                                      project_links=project_links)
        return self.projects[name]

    def submit_package(self, package=None, project=None):
        if not project:
            project = self.project
        request = Request(source_package=package, target_project=project)
        self.requests.append(request)
        return request

    def create_submit_request(self, project, package, text=None):
        project = self.create_project(project)
        package = Package(name=package, project=project)
        package.create_commit(text=text)
        return self.submit_package(package)

    def create_staging(self, suffix, freeze=False, rings=None):
        staging_key = 'staging:{}'.format(suffix)
        # do not reattach if already present
        if not staging_key in self.projects:
            staging_name = self.project + ':Staging:' + suffix
            staging = Project(staging_name, create=False)
            url = osc.core.makeurl(APIURL, ['staging', self.project, 'staging_projects'])
            data = '<workflow><staging_project>{}</staging_project></workflow>'
            osc.core.http_POST(url, data=data.format(staging_name))
            self.projects[staging_key] = staging
        else:
            staging = self.projects[staging_key]

        project_links = []
        if rings == 0:
            project_links.append(self.project + ":Rings:0-Bootstrap")
        if rings == 1 or rings == 0:
            project_links.append(self.project + ":Rings:1-MinimalX")
        staging.update_meta(project_links=project_links)

        if freeze:
            FreezeCommand(self.api).perform(staging.name)

        return staging

    def __del__(self):
        if not self.api:
            return
        try:
            self.remove()
        except:
            # normally exceptions in destructors are ignored but a info
            # message is displayed. Make this a little more useful by
            # printing it into the capture log
            traceback.print_exc(None, sys.stdout)

    def remove(self):
        print('deleting staging workflow')
        for project in self.projects.values():
            project.remove()
        for request in self.requests:
            request.revoke()
        for group in self.groups:
            url = osc.core.makeurl(APIURL, ['group', group])
            try:
                osc.core.http_DELETE(url)
            except HTTPError:
                pass
        print('done')
        if hasattr(self.api, '_invalidate_all'):
            self.api._invalidate_all()

class Project(object):
    def __init__(self, name, reviewer={}, maintainer={}, project_links=[], create=True):
        self.name = name
        self.packages = []

        if not create:
            return

        self.update_meta(reviewer, maintainer, project_links)

    def update_meta(self, reviewer={}, maintainer={}, project_links=[]):
        meta = """
            <project name="{0}">
              <title></title>
              <description></description>
            </project>""".format(self.name)

        root = ET.fromstring(meta)
        for group in reviewer.get('groups', []):
            ET.SubElement(root, 'group', { 'groupid': group, 'role': 'reviewer'} )
        for group in reviewer.get('users', []):
            ET.SubElement(root, 'person', { 'userid': group, 'role': 'reviewer'} )
        # TODO: avoid this duplication
        for group in maintainer.get('groups', []):
            ET.SubElement(root, 'group', { 'groupid': group, 'role': 'maintainer'} )
        for group in maintainer.get('users', []):
            ET.SubElement(root, 'person', { 'userid': group, 'role': 'maintainer'} )

        for link in project_links:
            ET.SubElement(root, 'link', { 'project': link })
        self.custom_meta(ET.tostring(root))

    def add_package(self, package):
        self.packages.append(package)

    def custom_meta(self, meta):
        url = osc.core.make_meta_url('prj', self.name, APIURL)
        osc.core.http_PUT(url, data=meta)

    def remove(self):
        if not self.name:
            return
        print('deleting project', self.name)
        for package in self.packages:
            package.remove()

        url = osc.core.makeurl(APIURL, ['source', self.name], {'force': 1})
        try:
            osc.core.http_DELETE(url)
        except HTTPError as e:
            if e.code != 404:
                raise e
        self.name = None

    def __del__(self):
        self.remove()

class Package(object):
    def __init__(self, name, project, devel_project=None):
        self.name = name
        self.project = project

        meta = """
            <package project="{1}" name="{0}">
              <title></title>
              <description></description>
            </package>""".format(self.name, self.project.name)

        if devel_project:
            root = ET.fromstring(meta)
            ET.SubElement(root, 'devel', { 'project': devel_project })
            meta = ET.tostring(root)

        url = osc.core.make_meta_url('pkg', (self.project.name, self.name), APIURL)
        osc.core.http_PUT(url, data=meta)
        print('created {}/{}'.format(self.project.name, self.name))
        self.project.add_package(self)

    # delete from instance
    def __del__(self):
        self.remove()

    def create_file(self, filename, data=''):
        url = osc.core.makeurl(APIURL, ['source', self.project.name, self.name, filename])
        osc.core.http_PUT(url, data=data)

    def remove(self):
        if not self.project:
            return
        print('deleting package', self.project.name, self.name)
        url = osc.core.makeurl(APIURL, ['source', self.project.name, self.name])
        try:
            osc.core.http_DELETE(url)
        except HTTPError as e:
            if e.code != 404:
                raise e
        self.project = None

    def create_commit(self, text=None, filename='README'):
        url = osc.core.makeurl(APIURL, ['source', self.project.name, self.name, filename])
        if not text:
            text = ''.join([random.choice(string.ascii_letters) for i in range(40)])
        osc.core.http_PUT(url, data=text)

class Request(object):
    def __init__(self, source_package, target_project):
        self.source_package = source_package
        self.target_project = target_project

        self.reqid = osc.core.create_submit_request(APIURL,
                                 src_project=self.source_package.project.name,
                                 src_package=self.source_package.name,
                                 dst_project=self.target_project)
        self.revoked = False

        print('created submit request {}/{} -> {}'.format(
            self.source_package.project.name, self.source_package.name, self.target_project))

    def __del__(self):
        self.revoke()

    def revoke(self):
        if self.revoked: return
        self.change_state('revoked')
        self.revoked = True

    def change_state(self, state):
        print(f'changing request state of {self.reqid} to {state}')

        try:
            request_state_change(APIURL, self.reqid, state)
        except HTTPError as e:
            # may fail if already accepted/declined in tests or project deleted
            if e.code != 403 and e.code != 404:
                raise e

    def _translate_review(self, review):
        ret = {'state': review.get('state')}
        for type in ['by_project', 'by_package', 'by_user', 'by_group']:
            if not review.get(type):
                continue
            ret[type] = review.get(type)
        return ret

    def reviews(self):
        ret = []
        for review in self.xml().findall('.//review'):
            ret.append(self._translate_review(review))
        return ret

    def xml(self):
        url = osc.core.makeurl(APIURL, ['request', self.reqid])
        return ET.parse(osc.core.http_GET(url))
