import flask
from flask_jwt_extended import create_access_token
from flask_jwt_extended import get_jwt_identity
from flask_jwt_extended import JWTManager
from flask_jwt_extended import jwt_required
import flask_restful
from flask_restful import fields
from flask_restful import marshal_with
import ipaddress
import json
import logging
from logging import handlers as logging_handlers
import os
import re
import requests
import setproctitle
import sys
import traceback
import uuid

from oslo_concurrency import processutils

from shakenfist import config
from shakenfist import db
from shakenfist import etcd
from shakenfist import images
from shakenfist import net
from shakenfist import scheduler
from shakenfist import util
from shakenfist import virt


LOG = logging.getLogger(__file__)
LOG.setLevel(logging.DEBUG)
LOG.addHandler(logging_handlers.SysLogHandler(address='/dev/log'))


TESTING = False
SCHEDULER = None


def error(status_code, message):
    global TESTING

    body = {
        'error': message,
        'status': status_code
    }

    if TESTING or config.parsed.get('INCLUDE_TRACEBACKS') == '1':
        _, _, tb = sys.exc_info()
        if tb:
            body['traceback'] = traceback.format_exc()

    resp = flask.Response(json.dumps(body),
                          mimetype='application/json')
    resp.status_code = status_code
    LOG.error('Returning API error: %d, %s\n    %s'
              % (status_code, message, '\n    '.join(body.get('traceback', '').split('\n'))))
    return resp


def flask_get_post_body():
    j = {}
    try:
        j = flask.request.get_json(force=True)
    except Exception:
        if flask.request.data:
            try:
                j = json.loads(flask.request.data)
            except Exception:
                pass
    return j


def generic_wrapper(func):
    def wrapper(*args, **kwargs):
        try:
            LOG.info('External API request: %s' % flask.request)
            j = flask_get_post_body()

            if j:
                for key in j:
                    if key == 'uuid':
                        destkey = 'passed_uuid'
                    else:
                        destkey = key
                    kwargs[destkey] = j[key]

            LOG.info('External API request: %s %s %s' % (func, args, kwargs))
            return func(*args, **kwargs)
        except Exception:
            return error(500, 'server error')
    return wrapper


class Resource(flask_restful.Resource):
    method_decorators = [generic_wrapper]


def caller_is_admin(func):
    # Ensure only users in the "all" namespace can call this method
    def wrapper(*args, **kwargs):
        if get_jwt_identity() != 'all':
            return error(401, 'Unauthorized')

        return func(*args, **kwargs)
    return wrapper


def arg_is_instance_uuid(func):
    # Method uses the instance from the db
    def wrapper(*args, **kwargs):
        if 'instance_uuid' in kwargs:
            kwargs['instance_from_db'] = db.get_instance(
                kwargs['instance_uuid'])
        if not kwargs.get('instance_from_db'):
            return error(404, 'instance not found')

        return func(*args, **kwargs)
    return wrapper


def arg_is_instance_uuid_as_virt(func):
    # Method uses the rehydrated instance
    def wrapper(*args, **kwargs):
        if 'instance_uuid' in kwargs:
            kwargs['instance_from_db_virt'] = virt.from_db(
                kwargs['instance_uuid']
            )
        if not kwargs.get('instance_from_db_virt'):
            return error(404, 'instance not found')

        return func(*args, **kwargs)
    return wrapper


def redirect_instance_request(func):
    # Redirect method to the hypervisor hosting the instance
    def wrapper(*args, **kwargs):
        i = kwargs.get('instance_from_db_virt')
        if i and i.db_entry['node'] != config.parsed.get('NODE_NAME'):
            url = 'http://%s:%d%s' % (i.db_entry['node'],
                                      config.parsed.get('API_PORT'),
                                      flask.request.environ['PATH_INFO'])
            r = requests.request(
                flask.request.environ['REQUEST_METHOD'], url,
                data=json.dumps(flask_get_post_body()),
                headers={'Authorization': flask.request.headers.get('Authorization'),
                         'User-Agent': util.get_user_agent()})

            LOG.info('Proxied %s %s returns: %d, %s'
                     % (flask.request.environ['REQUEST_METHOD'], url,
                        r.status_code, r.text))
            resp = flask.Response(r.text,
                                  mimetype='application/json')
            resp.status_code = r.status_code
            return resp

        return func(*args, **kwargs)
    return wrapper


def arg_is_network_uuid(func):
    # Method uses the network from the db
    def wrapper(*args, **kwargs):
        if 'network_uuid' in kwargs:
            kwargs['network_from_db'] = db.get_network(
                kwargs['network_uuid'])
        if not kwargs.get('network_from_db'):
            return error(404, 'network not found')

        return func(*args, **kwargs)
    return wrapper


def redirect_to_network_node(func):
    # Redirect method to the network node
    def wrapper(*args, **kwargs):
        if config.parsed.get('NODE_IP') != config.parsed.get('NETWORK_NODE_IP'):
            admin_token = util.get_admin_api_token(
                'http://%s:%d' % (config.parsed.get('NETWORK_NODE_IP'),
                                  config.parsed.get('API_PORT')))
            r = requests.request(
                flask.request.environ['REQUEST_METHOD'],
                'http://%s:%d%s'
                % (config.parsed.get('NETWORK_NODE_IP'),
                   config.parsed.get('API_PORT'),
                   flask.request.environ['PATH_INFO']),
                data=json.dumps(flask.request.get_json()),
                headers={'Authorization': admin_token,
                         'User-Agent': util.get_user_agent()})

            LOG.info('Returning proxied request: %d, %s'
                     % (r.status_code, r.text))
            resp = flask.Response(r.text,
                                  mimetype='application/json')
            resp.status_code = r.status_code
            return resp

        return func(*args, **kwargs)
    return wrapper


app = flask.Flask(__name__)
api = flask_restful.Api(app, catch_all_404s=False)
app.config['JWT_SECRET_KEY'] = config.parsed.get('AUTH_SECRET_SEED')
jwt = JWTManager(app)


@app.before_request
def log_request_info():
    output = 'API request headers:\n'
    for header, value in flask.request.headers:
        output += '    %s: %s\n' % (header, value)
    output += 'API request body: %s' % flask.request.get_data()

    app.logger.info(output)


class Root(Resource):
    def get(self):
        resp = flask.Response(
            'Shaken Fist REST API service',
            mimetype='text/plain')
        resp.status_code = 200
        return resp


class Auth(Resource):
    def _get_password(self, namespace):
        rec = etcd.get('passwords', None, namespace)
        if rec:
            return rec.get('passwords', [])
        return []

    def post(self, namespace=None, password=None):
        if not namespace:
            return error(400, 'Missing namespace in request')
        if not password:
            return error(400, 'Missing password in request')

        if password not in self._get_password(namespace):
            return error(401, 'Unauthorized')
        return {'access_token': create_access_token(identity=namespace)}


class Instance(Resource):
    @jwt_required
    @arg_is_instance_uuid
    def get(self, instance_uuid=None, instance_from_db=None):
        db.add_event('instance', instance_uuid, 'api', 'get', None, None)
        return instance_from_db

    @jwt_required
    @arg_is_instance_uuid_as_virt
    @redirect_instance_request
    def delete(self, instance_uuid=None, instance_from_db_virt=None):
        db.add_event('instance', instance_uuid, 'api', 'delete', None, None)

        instance_networks = []
        for iface in list(db.get_instance_interfaces(instance_uuid)):
            if not iface['network_uuid'] in instance_networks:
                instance_networks.append(iface['network_uuid'])
                db.update_network_interface_state(iface['uuid'], 'deleted')

        host_networks = []
        for inst in list(db.get_instances(only_node=config.parsed.get('NODE_NAME'))):
            if not inst['uuid'] == instance_uuid:
                for iface in db.get_instance_interfaces(inst['uuid']):
                    if not iface['network_uuid'] in host_networks:
                        host_networks.append(iface['network_uuid'])

        instance_from_db_virt.delete()

        for network in instance_networks:
            n = net.from_db(network)
            if n:
                if network in host_networks:
                    with util.RecordedOperation('deallocate ip address',
                                                instance_from_db_virt) as _:
                        n.update_dhcp()
                else:
                    with util.RecordedOperation('remove network', n) as _:
                        n.delete()


class Instances(Resource):
    @jwt_required
    def get(self, all=False):
        return list(db.get_instances(all=all))

    @jwt_required
    def post(self, name=None, cpus=None, memory=None, network=None,
             disk=None, ssh_key=None, user_data=None, placed_on=None, instance_uuid=None):
        global SCHEDULER

        # We need to santize the name so its safe for DNS
        name = re.sub(r'([^a-zA-Z0-9_\-])', '', name)

        # The instance needs to exist in the DB before network interfaces are created
        if not instance_uuid:
            instance_uuid = str(uuid.uuid4())
            db.add_event('instance', instance_uuid,
                         'uuid allocated', None, None, None)

        # Create instance object
        instance = virt.from_db(instance_uuid)
        if not instance:
            instance = virt.from_definition(
                uuid=instance_uuid,
                name=name,
                disks=disk,
                memory_mb=memory * 1024,
                vcpus=cpus,
                ssh_key=ssh_key,
                user_data=user_data
            )

        if not SCHEDULER:
            SCHEDULER = scheduler.Scheduler()

        # Have we been placed?
        if not placed_on:
            candidates = SCHEDULER.place_instance(instance, network)
            if len(candidates) == 0:
                db.add_event('instance', instance_uuid,
                             'schedule', 'failed', None, 'insufficient resources')
                db.update_instance_state(instance_uuid, 'error')
                return error(507, 'Insufficient capacity')

            placed_on = candidates[0]
            db.place_instance(instance_uuid, placed_on)
            db.add_event('instance', instance_uuid,
                         'placement', None, None, placed_on)

        else:
            try:
                candidates = SCHEDULER.place_instance(
                    instance, network, candidates=[placed_on])
                if len(candidates) == 0:
                    db.add_event('instance', instance_uuid,
                                 'schedule', 'failed', None, 'insufficient resources')
                    db.update_instance_state(instance_uuid, 'error')
                    return error(507, 'Insufficient capacity')
            except scheduler.CandidateNodeNotFoundException as e:
                return error(404, 'Node not found: %s' % e)

        # Have we been placed on a different node?
        if not placed_on == config.parsed.get('NODE_NAME'):
            body = flask_get_post_body()
            body['placed_on'] = placed_on
            body['instance_uuid'] = instance_uuid

            r = requests.request('POST',
                                 'http://%s:%d/instances'
                                 % (placed_on,
                                    config.parsed.get('API_PORT')),
                                 data=json.dumps(body),
                                 headers={'User-Agent': util.get_user_agent()})

            LOG.info('Returning proxied request: %d, %s'
                     % (r.status_code, r.text))
            resp = flask.Response(r.text,
                                  mimetype='application/json')
            resp.status_code = r.status_code
            return resp

        # Check we can get the required IPs
        nets = {}
        allocations = {}

        def error_with_cleanup(status_code, message):
            for network_uuid in allocations:
                n = net.from_db(network_uuid)
                for addr, _ in allocations[network_uuid]:
                    with etcd.get_lock('sf/ipmanager/%s' % n.uuid, ttl=120) as _:
                        ipm = db.get_ipmanager(n.uuid)
                        ipm.release(addr)
                        db.persist_ipmanager(n.uuid, ipm.save())
            return error(status_code, message)

        order = 0
        for netdesc in network:
            if 'network_uuid' not in netdesc or not netdesc['network_uuid']:
                error_with_cleanup(404, 'network not specified')

            if netdesc['network_uuid'] not in nets:
                n = net.from_db(netdesc['network_uuid'])
                if not n:
                    error_with_cleanup(
                        404, 'network %s not found' % netdesc['network_uuid'])
                nets[netdesc['network_uuid']] = n
                n.create()

            with etcd.get_lock('sf/ipmanager/%s' % netdesc['network_uuid'],
                               ttl=120) as _:
                allocations.setdefault(netdesc['network_uuid'], [])
                ipm = db.get_ipmanager(netdesc['network_uuid'])
                if 'address' not in netdesc or not netdesc['address']:
                    netdesc['address'] = ipm.get_random_free_address()
                else:
                    if not ipm.reserve(netdesc['address']):
                        error_with_cleanup(409, 'address %s in use' %
                                           netdesc['address'])
                db.persist_ipmanager(netdesc['network_uuid'], ipm.save())
                allocations[netdesc['network_uuid']].append(
                    (netdesc['address'], order))

            if 'model' not in netdesc or not netdesc['model']:
                netdesc['model'] = 'virtio'

            db.create_network_interface(
                str(uuid.uuid4()), netdesc, instance_uuid, order)

            order += 1

        # Now we can start the instance
        with util.RecordedOperation('ensure networks exist', instance) as _:
            for network_uuid in nets:
                n = nets[network_uuid]
                n.ensure_mesh()
                n.update_dhcp()

        with util.RecordedOperation('instance creation', instance) as _:
            instance.create()

        for iface in db.get_instance_interfaces(instance.db_entry['uuid']):
            db.update_network_interface_state(iface['uuid'], 'created')

        return db.get_instance(instance_uuid)


class InstanceInterfaces(Resource):
    @jwt_required
    @arg_is_instance_uuid
    def get(self, instance_uuid=None, instance_from_db=None):
        db.add_event('instance', instance_uuid,
                     'api', 'get interfaces', None, None)
        return list(db.get_instance_interfaces(instance_uuid))


class InstanceEvents(Resource):
    @jwt_required
    @arg_is_instance_uuid
    def get(self, instance_uuid=None, instance_from_db=None):
        db.add_event('instance', instance_uuid,
                     'api', 'get events', None, None)
        return list(db.get_events('instance', instance_uuid))


class InstanceSnapshot(Resource):
    @jwt_required
    @arg_is_instance_uuid_as_virt
    @redirect_instance_request
    def post(self, instance_uuid=None, instance_from_db_virt=None, all=None):
        snap_uuid = instance_from_db_virt.snapshot(all=all)
        db.add_event('instance', instance_uuid,
                     'api', 'snapshot (all=%s)' % all,
                     None, snap_uuid)
        db.add_event('snapshot', snap_uuid,
                     'api', 'create', None, None)
        return snap_uuid

    @jwt_required
    @arg_is_instance_uuid
    def get(self, instance_uuid=None, instance_from_db=None):
        db.add_event('instance', instance_uuid,
                     'api', 'get', None, None)
        out = []
        for snap in db.get_instance_snapshots(instance_uuid):
            snap['created'] = snap['created']
            out.append(snap)
        return out


class InstanceRebootSoft(Resource):
    @jwt_required
    @arg_is_instance_uuid_as_virt
    @redirect_instance_request
    def post(self, instance_uuid=None, instance_from_db_virt=None):
        db.add_event('instance', instance_uuid,
                     'api', 'soft reboot', None, None)
        return instance_from_db_virt.reboot(hard=False)


class InstanceRebootHard(Resource):
    @jwt_required
    @arg_is_instance_uuid_as_virt
    @redirect_instance_request
    def post(self, instance_uuid=None, instance_from_db_virt=None):
        db.add_event('instance', instance_uuid,
                     'api', 'hard reboot', None, None)
        return instance_from_db_virt.reboot(hard=True)


class InstancePowerOff(Resource):
    @jwt_required
    @arg_is_instance_uuid_as_virt
    @redirect_instance_request
    def post(self, instance_uuid=None, instance_from_db_virt=None):
        db.add_event('instance', instance_uuid,
                     'api', 'poweroff', None, None)
        return instance_from_db_virt.power_off()


class InstancePowerOn(Resource):
    @jwt_required
    @arg_is_instance_uuid_as_virt
    @redirect_instance_request
    def post(self, instance_uuid=None, instance_from_db_virt=None):
        db.add_event('instance', instance_uuid,
                     'api', 'poweron', None, None)
        return instance_from_db_virt.power_on()


class InstancePause(Resource):
    @jwt_required
    @arg_is_instance_uuid_as_virt
    @redirect_instance_request
    def post(self, instance_uuid=None, instance_from_db_virt=None):
        db.add_event('instance', instance_uuid, 'api', 'pause', None, None)
        return instance_from_db_virt.pause()


class InstanceUnpause(Resource):
    @jwt_required
    @arg_is_instance_uuid_as_virt
    @redirect_instance_request
    def post(self, instance_uuid=None, instance_from_db_virt=None):
        db.add_event('instance', instance_uuid,
                     'api', 'unpause', None, None)
        return instance_from_db_virt.unpause()


class InterfaceFloat(Resource):
    @jwt_required
    @redirect_to_network_node
    def post(self, interface_uuid=None):
        db.add_event('interface', interface_uuid,
                     'api', 'float', None, None)
        ni = db.get_interface(interface_uuid)
        if not ni:
            return error(404, 'network interface not found')

        if ni['floating']:
            return error(409, 'this interface already has a floating ip')

        n = net.from_db(ni['network_uuid'])
        if not n:
            return error(404, 'network not found')

        float_net = net.from_db('floating')
        if not float_net:
            return error(404, 'floating network not found')

        with etcd.get_lock('sf/ipmanager/floating', ttl=120) as _:
            ipm = db.get_ipmanager('floating')
            addr = ipm.get_random_free_address()
            db.persist_ipmanager('floating', ipm.save())

        db.add_floating_to_interface(ni['uuid'], addr)
        n.add_floating_ip(addr, ni['ipv4'])


class InterfaceDefloat(Resource):
    @jwt_required
    @redirect_to_network_node
    def post(self, interface_uuid=None):
        db.add_event('interface', interface_uuid,
                     'api', 'defloat', None, None)
        ni = db.get_interface(interface_uuid)
        if not ni:
            return error(404, 'network interface not found')

        if not ni['floating']:
            return error(409, 'this interface does not have a floating ip')

        n = net.from_db(ni['network_uuid'])
        if not n:
            return error(404, 'network not found')

        float_net = net.from_db('floating')
        if not float_net:
            return error(404, 'floating network not found')

        with etcd.get_lock('sf/ipmanager/floating', ttl=120) as _:
            ipm = db.get_ipmanager('floating')
            ipm.release(ni['floating'])
            db.persist_ipmanager('floating', ipm.save())

        db.remove_floating_from_interface(ni['uuid'])
        n.remove_floating_ip(ni['floating'], ni['ipv4'])


class Image(Resource):
    @jwt_required
    @caller_is_admin
    def post(self, url=None):
        db.add_event('image', url, 'api', 'cache', None, None)

        with util.RecordedOperation('cache image', url) as _:
            images.fetch_image(url)


class Network(Resource):
    @jwt_required
    @arg_is_network_uuid
    def get(self, network_uuid=None, network_from_db=None):
        db.add_event('network', network_uuid, 'api', 'get', None, None)
        del network_from_db['ipmanager']
        return network_from_db

    @jwt_required
    @arg_is_network_uuid
    @redirect_to_network_node
    def delete(self, network_uuid=None, network_from_db=None):
        db.add_event('network', network_uuid, 'api', 'delete', None, None)
        if network_uuid == 'floating':
            return error(403, 'you cannot delete the floating network')

        # We only delete unused networks
        if len(list(db.get_network_interfaces(network_uuid))) > 0:
            return error(403, 'you cannot delete an in use network')

        n = net.from_db(network_uuid)
        n.remove_dhcp()
        n.delete()

        if n.floating_gateway:
            with etcd.get_lock('sf/ipmanager/floating', ttl=120) as _:
                ipm = db.get_ipmanager('floating')
                ipm.release(n.floating_gateway)
                db.persist_ipmanager('floating', ipm.save())

        db.update_network_state(network_uuid, 'deleted')


class Networks(Resource):
    @jwt_required
    @marshal_with({
        'uuid': fields.String,
        'vxlan_id': fields.Integer,
        'netblock': fields.String,
        'provide_dhcp': fields.Boolean,
        'provide_nat': fields.Boolean,
        'owner': fields.String,
        'name': fields.String,
    })
    def get(self, all=False):
        return list(db.get_networks(all=all))

    @jwt_required
    def post(self, netblock=None, provide_dhcp=None, provide_nat=None, name=None):
        try:
            ipaddress.ip_network(netblock)
        except ValueError as e:
            return error(400, 'cannot parse netblock: %s' % e)

        network = db.allocate_network(netblock, provide_dhcp,
                                      provide_nat, name, get_jwt_identity())
        db.add_event('network', network['uuid'],
                     'api', 'create', None, None)

        # Networks should immediately appear on the network node
        if config.parsed.get('NODE_IP') == config.parsed.get('NETWORK_NODE_IP'):
            n = net.from_db(network['uuid'])
            if not n:
                return error(404, 'network not found')

            n.create()
            n.ensure_mesh()
        else:
            admin_token = util.get_admin_api_token(
                'http://%s:%d' % (config.parsed.get('NETWORK_NODE_IP'),
                                  config.parsed.get('API_PORT')))
            requests.request(
                'put',
                ('http://%s:%d/deploy_network_node'
                 % (config.parsed.get('NETWORK_NODE_IP'),
                    config.parsed.get('API_PORT'))),
                data=json.dumps({'uuid': network['uuid']}),
                headers={'Authorization': admin_token,
                         'User-Agent': util.get_user_agent()})

        db.update_network_state(network['uuid'], 'created')
        return network


class NetworkEvents(Resource):
    @jwt_required
    @arg_is_network_uuid
    def get(self, network_uuid=None, network_from_db=None):
        db.add_event('network', network_uuid,
                     'api', 'get events', None, None)
        return list(db.get_events('network', network_uuid))


class Nodes(Resource):
    @jwt_required
    @caller_is_admin
    @marshal_with({
        'name': fields.String(attribute='fqdn'),
        'ip': fields.String,
        'lastseen': fields.Float,
    })
    def get(self):
        return list(db.get_nodes())


# Internal APIs


class DeployNetworkNode(Resource):
    @jwt_required
    @caller_is_admin
    @redirect_to_network_node
    def put(self, passed_uuid=None):
        n = net.from_db(passed_uuid)
        if not n:
            return error(404, 'network not found')

        n.create()
        n.ensure_mesh()


class UpdateDHCP(Resource):
    @jwt_required
    @caller_is_admin
    @redirect_to_network_node
    def put(self, passed_uuid=None):
        n = net.from_db(passed_uuid)
        if not n:
            return error(404, 'network not found')

        n.update_dhcp()


class RemoveDHCP(Resource):
    @jwt_required
    @caller_is_admin
    @redirect_to_network_node
    def put(self, passed_uuid=None):
        n = net.from_db(passed_uuid)
        if not n:
            return error(404, 'network not found')

        n.remove_dhcp()


api.add_resource(Root, '/')
api.add_resource(Auth, '/auth')
api.add_resource(Instances, '/instances')
api.add_resource(Instance, '/instances/<instance_uuid>')
api.add_resource(InstanceEvents, '/instances/<instance_uuid>/events')
api.add_resource(InstanceInterfaces, '/instances/<instance_uuid>/interfaces')
api.add_resource(InstanceSnapshot, '/instances/<instance_uuid>/snapshot')
api.add_resource(InstanceRebootSoft, '/instances/<instance_uuid>/rebootsoft')
api.add_resource(InstanceRebootHard, '/instances/<instance_uuid>/reboothard')
api.add_resource(InstancePowerOff, '/instances/<instance_uuid>/poweroff')
api.add_resource(InstancePowerOn, '/instances/<instance_uuid>/poweron')
api.add_resource(InstancePause, '/instances/<instance_uuid>/pause')
api.add_resource(InstanceUnpause, '/instances/<instance_uuid>/unpause')
api.add_resource(InterfaceFloat, '/interfaces/<interface_uuid>/float')
api.add_resource(InterfaceDefloat, '/interfaces/<interface_uuid>/defloat')
api.add_resource(Image, '/images')
api.add_resource(Networks, '/networks')
api.add_resource(Network, '/networks/<network_uuid>')
api.add_resource(NetworkEvents, '/networks/<network_uuid>/events')
api.add_resource(Nodes, '/nodes')

api.add_resource(DeployNetworkNode, '/deploy_network_node')
api.add_resource(UpdateDHCP, '/update_dhcp')
api.add_resource(RemoveDHCP, '/remove_dhcp')


class monitor(object):
    def __init__(self):
        setproctitle.setproctitle('sf api')

    def run(self):
        processutils.execute(
            ('gunicorn3 --workers 10 --bind 0.0.0.0:%d '
             '--log-syslog --log-syslog-prefix sf '
             '--name "sf api" '
             'shakenfist.external_api.app:app'
             % config.parsed.get('API_PORT')),
            shell=True, env_variables=os.environ)