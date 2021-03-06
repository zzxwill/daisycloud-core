# Copyright 2013 OpenStack Foundation
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
/hosts endpoint for Daisy v1 API
"""
import subprocess
import re
from oslo_config import cfg
from oslo_log import log as logging
from webob.exc import HTTPBadRequest
from webob.exc import HTTPConflict
from webob.exc import HTTPForbidden
from webob.exc import HTTPNotFound
from webob import Response
from collections import Counter
from webob.exc import HTTPServerError
from daisy.api import policy
import daisy.api.v1
from daisy.api.v1 import controller
from daisy.api.v1 import filters
from daisy.common import exception
from daisy.common import property_utils
from daisy.common import utils
from daisy.common import wsgi
from daisy import i18n
from daisy import notifier
import daisy.registry.client.v1.api as registry
import threading
import daisy.api.backends.common as daisy_cmn
import ConfigParser

LOG = logging.getLogger(__name__)
_ = i18n._
_LE = i18n._LE
_LI = i18n._LI
_LW = i18n._LW
SUPPORTED_PARAMS = daisy.api.v1.SUPPORTED_PARAMS
SUPPORTED_FILTERS = daisy.api.v1.SUPPORTED_FILTERS
ACTIVE_IMMUTABLE = daisy.api.v1.ACTIVE_IMMUTABLE

CONF = cfg.CONF
CONF.import_opt('disk_formats', 'daisy.common.config', group='image_format')
CONF.import_opt('container_formats', 'daisy.common.config',
                group='image_format')
CONF.import_opt('image_property_quota', 'daisy.common.config')
config = ConfigParser.ConfigParser()
config.read("/home/daisy_install/daisy.conf")
ML2_TYPE = ['ovs', 'dvs', 'ovs,sriov(macvtap)', 'ovs,sriov(direct)', 'sriov(macvtap)', 'sriov(direct)']

class Controller(controller.BaseController):
    """
    WSGI controller for hosts resource in Daisy v1 API

    The hosts resource API is a RESTful web service for host data. The API
    is as follows::

        GET  /nodes -- Returns a set of brief metadata about hosts
        GET  /nodes -- Returns a set of detailed metadata about
                              hosts
        HEAD /nodes/<ID> -- Return metadata about an host with id <ID>
        GET  /nodes/<ID> -- Return host data for host with id <ID>
        POST /nodes -- Store host data and return metadata about the
                        newly-stored host
        PUT  /nodes/<ID> -- Update host metadata and/or upload host
                            data for a previously-reserved host
        DELETE /nodes/<ID> -- Delete the host with id <ID>
    """
    support_resource_type = ['baremetal', 'server', 'docker']
    
    def __init__(self):
        self.notifier = notifier.Notifier()
        registry.configure_registry_client()
        self.policy = policy.Enforcer()
        if property_utils.is_property_protection_enabled():
            self.prop_enforcer = property_utils.PropertyRules(self.policy)
        else:
            self.prop_enforcer = None

    def _enforce(self, req, action, target=None):
        """Authorize an action against our policies"""
        if target is None:
            target = {}
        try:
            self.policy.enforce(req.context, action, target)
        except exception.Forbidden:
            raise HTTPForbidden()

    def _raise_404_if_network_deleted(self, req, network_id):
        network = self.get_network_meta_or_404(req, network_id)
        if network is None or network['deleted']:
            msg = _("Network with identifier %s has been deleted.") % network_id
            raise HTTPNotFound(msg)

    def _raise_404_if_cluster_deleted(self, req, cluster_id):
        cluster = self.get_cluster_meta_or_404(req, cluster_id)
        if cluster is None or cluster['deleted']:
            msg = _("Cluster with identifier %s has been deleted.") % cluster_id
            raise HTTPNotFound(msg)

    def _raise_404_if_role_deleted(self, req, role_id):
        role = self.get_role_meta_or_404(req, role_id)
        if role is None or role['deleted']:
            msg = _("Cluster with identifier %s has been deleted.") % role_id
            raise HTTPNotFound(msg)


    def _get_filters(self, req):
        """
        Return a dictionary of query param filters from the request

        :param req: the Request object coming from the wsgi layer
        :retval a dict of key/value filters
        """
        query_filters = {}
        for param in req.params:
            if param in SUPPORTED_FILTERS:
                query_filters[param] = req.params.get(param)
                if not filters.validate(param, query_filters[param]):
                    raise HTTPBadRequest(_('Bad value passed to filter '
                                           '%(filter)s got %(val)s')
                                         % {'filter': param,
                                            'val': query_filters[param]})
        return query_filters

    def _get_query_params(self, req):
        """
        Extracts necessary query params from request.

        :param req: the WSGI Request object
        :retval dict of parameters that can be used by registry client
        """
        params = {'filters': self._get_filters(req)}

        for PARAM in SUPPORTED_PARAMS:
            if PARAM in req.params:
                params[PARAM] = req.params.get(PARAM)
        return params
    
    def check_bond_slaves_validity(self, bond_slaves_lists, ether_nic_names_list):
        '''
        members in bond slaves must be in ether_nic_names_list
        len(set(bond_slaves)) == 2, and can not be overlap between slaves members
        bond_slaves_lists: [[name1,name2], [name1,name2], ...]
        ether_nic_names_list: [name1, name2, ...]
        '''
        for bond_slaves in bond_slaves_lists:
            LOG.warn('bond_slaves: %s' % bond_slaves)
            if len(set(bond_slaves)) != 2:
                LOG.error('set(bond_slaves: %s' % set(bond_slaves))
                msg = (_("Bond slaves(%s) must be different nic and existed in ether nics in pairs." % bond_slaves))
                LOG.error(msg)
                raise HTTPForbidden(msg)
            if not set(bond_slaves).issubset(set(ether_nic_names_list)):
                msg = (_("Pay attention: illegal ether nic existed in bond slaves(%s)." % bond_slaves))
                LOG.error(msg)
                raise HTTPForbidden(msg)
    def validate_ip_format(self, ip_str):
        '''
        valid ip_str format = '10.43.178.9'
        invalid ip_str format : '123. 233.42.12', spaces existed in field
                                '3234.23.453.353', out of range
                                '-2.23.24.234', negative number in field
                                '1.2.3.4d', letter in field
                                '10.43.1789', invalid format
        '''
        valid_fromat = False
        if ip_str.count('.') == 3 and \
            all(num.isdigit() and 0<=int(num)<256 for num in ip_str.rstrip().split('.')):
            valid_fromat = True
        if valid_fromat == False:
            msg = (_("%s invalid ip format!") % ip_str)
            LOG.error(msg)
            raise HTTPForbidden(msg)
            
    def _ip_into_int(self, ip):
        """
        Switch ip string to decimalism integer..
        :param ip: ip string
        :return: decimalism integer
        """
        return reduce(lambda x, y: (x<<8)+y, map(int, ip.split('.')))

    def _is_in_network_range(self, ip, network):
        """
        Check ip is in range
        :param ip: Ip will be checked, like:192.168.1.2.
        :param network: Ip range,like:192.168.0.0/24.
        :return: If ip in range,return True,else return False.
        """
        network = network.split('/')
        mask = ~(2**(32 - int(network[1])) - 1)
        return (self._ip_into_int(ip) & mask) == (self._ip_into_int(network[0]) & mask)
        
    def get_cluster_networks_info(self, req, cluster_id):
        '''
        get_cluster_networks_info by cluster id 
        '''
        all_networks = registry.get_all_networks(req.context)
        cluster_networks = [network for network in all_networks if network['cluster_id'] == cluster_id]
        return cluster_networks

    def _check_assigned_networks(self, req, cluster_id, assigned_networks):
        LOG.info("assigned_networks %s " % assigned_networks)
        cluster_networks = self.get_cluster_networks_info(req, cluster_id)
        list_of_assigned_networks = []
        for assigned_network in assigned_networks:
            LOG.info("assigned_network %s " % assigned_network)
            if not assigned_network.has_key('name') or not assigned_network['name']:
                msg = "assigned networks '%s' are invalid" % (assigned_networks)
                LOG.error(msg)
                raise HTTPBadRequest(explanation=msg,
                                        request=req,
                                        content_type="text/plain")
            network_info = [network for network in cluster_networks if network['name'] == assigned_network['name']]   
            if network_info and network_info[0]:
                network_cidr = network_info[0]['cidr']
                LOG.info("network_info %s " % network_info)
                if network_info[0]['network_type'] != 'PRIVATE':
                    if network_cidr:
                        if assigned_network.has_key('ip') and assigned_network['ip']:
                            self.validate_ip_format(assigned_network['ip'])
                            ip_in_cidr = self._is_in_network_range(assigned_network['ip'], network_cidr)
                            if not ip_in_cidr:
                                msg = (_("The ip '%s' for network '%s' is not in cidr range." % 
                                        (assigned_network['ip'], assigned_network['name'])))
                                raise HTTPBadRequest(explanation=msg)
                    else:
                        msg = "error, cidr of network '%s' is empty" % (assigned_network['name'])
                        LOG.error(msg)
                        raise HTTPBadRequest(explanation=msg,
                                                request=req,
                                                content_type="text/plain")
            else:
                msg = "can't find network named '%s' in cluster '%s'" % (assigned_network['name'], cluster_id)
                LOG.error(msg)
                raise HTTPBadRequest(explanation=msg,
                                        request=req,
                                        content_type="text/plain")
            list_of_assigned_networks.append(network_info[0])
        return list_of_assigned_networks

    def _compare_assigned_networks_of_interface(self, interface1, interface2):
        for network in interface1:
            for network_compare in interface2:
                if network['cidr'] == network_compare['cidr']:
                    return network['name'], network_compare['name']
        return False, False

    def _compare_assigned_networks_between_interfaces(
            self, interface_num, assigned_networks_of_interfaces):
        for interface_id in range(interface_num):
            for interface_id_compare in range(interface_id+1, interface_num):
                network1_name, network2_name = self.\
                    _compare_assigned_networks_of_interface\
                    (assigned_networks_of_interfaces[interface_id],
                     assigned_networks_of_interfaces[interface_id_compare])
                if network1_name and network2_name:
                    msg = (_('Network %s and network %s with same '
                             'cidr can not be assigned to different '
                             'interfaces.')) % (network1_name, network2_name)
                    raise HTTPBadRequest(explanation=msg)

    def _check_add_host_interfaces(self, req, host_meta):
        host_meta_interfaces = []
        if host_meta.has_key('interfaces'):
            host_meta_interfaces = list(eval(host_meta['interfaces']))
        else:
            return
            
        cluster_id = host_meta.get('cluster', None)   

        exist_id = self._verify_interface_among_hosts(req, host_meta)
        if exist_id:
            host_meta['id'] = exist_id
            self.update_host(req, exist_id, host_meta)
            LOG.info("<<<FUN:verify_interface, host:%s is already update.>>>" % exist_id)
            return {'host_meta': host_meta}
        
        if self._host_with_bad_pxe_info_in_params(host_meta):
            if cluster_id and host_meta.get('os_status', None) != 'active':
                msg = _("There is no nic for deployment, please choose "
                        "one interface to set it's 'is_deployment' True")
                raise HTTPServerError(explanation=msg)

        ether_nic_names_list = list()
        bond_nic_names_list = list()
        bond_slaves_lists = list()
        have_assigned_network = False
        have_ip_netmask = False
        assigned_networks_of_intefaces = []
        interface_num = 0
        for interface in host_meta_interfaces:
            assigned_networks_of_one_interface = []
            if interface.get('type', None) != 'bond' and not interface.get('mac', None):
                msg = _('The ether interface need a non-null mac ')
                raise HTTPBadRequest(explanation=msg,
                                     request=req,
                                     content_type="text/plain")
            if interface.get('type', None) != 'bond' and not interface.get('pci', None):
                msg = "The Interface need a non-null pci"
                LOG.error(msg)
                raise HTTPBadRequest(explanation=msg,
                                     request=req,
                                     content_type="text/plain")

            if interface.get('name', None):
                if interface.has_key('type') and interface['type'] == 'bond':
                    bond_nic_names_list.append(interface['name'])
                    if interface.get('slaves', None):
                        bond_slaves_lists.append(interface['slaves'])
                    else:
                        msg = (_("Slaves parameter can not be None when nic type was bond."))
                        LOG.error(msg)
                        raise HTTPForbidden(msg)
                else:  # type == ether or interface without type field
                    ether_nic_names_list.append(interface['name'])
            else:
                msg = (_("Nic name can not be None."))
                LOG.error(msg)
                raise HTTPForbidden(msg)
                
            if interface.has_key('is_deployment'):
                if interface['is_deployment'] == "True" or interface['is_deployment'] == True:
                    interface['is_deployment'] = 1
                else:
                    interface['is_deployment'] = 0

            if (interface.has_key('assigned_networks') and
                    interface['assigned_networks'] != [''] and 
                    interface['assigned_networks']):
                have_assigned_network = True
                if cluster_id:
                    assigned_networks_of_one_interface = self.\
                        _check_assigned_networks(req,
                                                 cluster_id,
                                                 interface['assigned_networks'])
                else:
                    msg = "cluster must be given first when network plane is allocated"
                    LOG.error(msg)
                    raise HTTPBadRequest(explanation=msg,
                                         request=req,
                                         content_type="text/plain")

            if (interface.has_key('ip') and interface['ip'] and
                interface.has_key('netmask') and interface['netmask']):
                have_ip_netmask = True
                
            if interface.has_key('mac') and interface.has_key('ip'):
                host_infos = registry.get_host_interface(req.context, host_meta)
                for host_info in host_infos:
                    if host_info.has_key('host_id'):
                        host_meta["id"] = host_info['host_id']
                        
            if interface.has_key('vswitch_type') and interface['vswitch_type'] != '' and interface['vswitch_type'] not in ML2_TYPE:
                msg = "vswitch_type %s is not supported" % interface['vswitch_type']
                raise HTTPBadRequest(explanation=msg, request=req,
                            content_type="text/plain")
            interface_num += 1
            assigned_networks_of_intefaces.\
                append(assigned_networks_of_one_interface)

        for interface_id in range(interface_num):
            for interface_id_compare in range(interface_id+1, interface_num):
                network1_name, network2_name = self.\
                    _compare_assigned_networks_of_interface\
                    (assigned_networks_of_intefaces[interface_id],
                     assigned_networks_of_intefaces[interface_id_compare])
                if network1_name and network2_name:
                    msg = (_('Network %s and network %s with same '
                             'cidr can not be assigned to different '
                             'interfaces.')) % (network1_name, network2_name)
                    raise HTTPBadRequest(explanation=msg)

        # when assigned_network is empty, ip must be config
        if not have_assigned_network:
            if not have_ip_netmask:
                msg = "ip and netmask must be given when network plane is not allocated"
                LOG.error(msg)
                raise HTTPBadRequest(explanation=msg,
                                     request=req,
                                     content_type="text/plain")
            
        # check bond slaves validity
        self.check_bond_slaves_validity(bond_slaves_lists, ether_nic_names_list)
        nic_name_list = ether_nic_names_list + bond_nic_names_list
        if len(set(nic_name_list)) != len(nic_name_list):
            msg = (_("Nic name must be unique."))
            LOG.error(msg)
            raise HTTPForbidden(msg)
    
    @utils.mutating
    def add_host(self, req, host_meta):
        """
        Adds a new host to Daisy

        :param req: The WSGI/Webob Request object
        :param image_meta: Mapping of metadata about host

        :raises HTTPBadRequest if x-host-name is missing
        """
        self._enforce(req, 'add_host')
        # if host is update in '_verify_interface_among_hosts', no need add host continue.
        cluster_id = host_meta.get('cluster', None)
        if cluster_id:
            self.get_cluster_meta_or_404(req, cluster_id)
        if host_meta.has_key('role') and host_meta['role']:
            role_id_list = []
            host_roles=[]
            if host_meta.has_key('cluster'):
                params = self._get_query_params(req)
                role_list = registry.get_roles_detail(req.context, **params)
                for role_name in role_list:
                    if role_name['cluster_id'] == host_meta['cluster']:
                        host_roles = list(eval(host_meta['role']))
                        for host_role in host_roles:
                            if role_name['name'] == host_role:
                                role_id_list.append(role_name['id'])
                                continue
                if len(role_id_list) != len(host_roles):
                    msg = "The role of params %s is not exist, please use the right name" % host_roles
                    raise HTTPBadRequest(explanation=msg,
                                         request=req,
                                         content_type="text/plain")
                host_meta['role'] = role_id_list
            else:
                msg = "cluster params is none"
                raise HTTPBadRequest(explanation=msg,
                                     request=req,
                                     content_type="text/plain")
        
        
        self._check_add_host_interfaces(req, host_meta)

        if host_meta.has_key('resource_type'):
            if host_meta['resource_type'] not in self.support_resource_type:
                msg = "resource type is not supported, please use it in %s" % self.support_resource_type
                raise HTTPBadRequest(explanation=msg,
                                     request=req,
                                     content_type="text/plain")
        else:
            host_meta['resource_type'] = 'baremetal'
            
        if host_meta.has_key('os_status'):
            if host_meta['os_status'] not in ['init', 'installing', 'active', 'failed', 'none']:
                msg = "os_status is not valid."
                raise HTTPBadRequest(explanation=msg,
                                     request=req,
                                     content_type="text/plain")
                
        if host_meta.has_key('ipmi_addr') and host_meta['ipmi_addr']:
            if not host_meta.has_key('ipmi_user'):
                host_meta['ipmi_user'] = 'zteroot'
            if not host_meta.has_key('ipmi_passwd'):
                host_meta['ipmi_passwd'] = 'superuser'

        host_meta = registry.add_host_metadata(req.context, host_meta)

        return {'host_meta': host_meta}

    @utils.mutating
    def delete_host(self, req, id):
        """
        Deletes a host from Daisy.

        :param req: The WSGI/Webob Request object
        :param image_meta: Mapping of metadata about host

        :raises HTTPBadRequest if x-host-name is missing
        """
        self._enforce(req, 'delete_host')
        try:
            registry.delete_host_metadata(req.context, id)
        except exception.NotFound as e:
            msg = (_("Failed to find host to delete: %s") %
                   utils.exception_to_str(e))
            LOG.error(msg)
            raise HTTPNotFound(explanation=msg,
                               request=req,
                               content_type="text/plain")
        except exception.Forbidden as e:
            msg = (_("Forbidden to delete host: %s") %
                   utils.exception_to_str(e))
            LOG.error(msg)
            raise HTTPForbidden(explanation=msg,
                                request=req,
                                content_type="text/plain")
        except exception.InUseByStore as e:
            msg = (_("Host %(id)s could not be deleted because it is in use: "
                     "%(exc)s") % {"id": id, "exc": utils.exception_to_str(e)})
            LOG.error(msg)
            raise HTTPConflict(explanation=msg,
                               request=req,
                               content_type="text/plain")
        else:
            #self.notifier.info('host.delete', host)
            params= {}
            discover_hosts = registry.get_discover_hosts_detail(req.context, **params)
            for host in discover_hosts:
                if host.get('host_id') == id:
                    LOG.info("delete discover host: %s" % id)
                    registry.delete_discover_host_metadata(req.context, host['id'])
            return Response(body='', status=200)

    @utils.mutating
    def get_host(self, req, id):
        """
        Returns metadata about an host in the HTTP headers of the
        response object

        :param req: The WSGI/Webob Request object
        :param id: The opaque host identifier

        :raises HTTPNotFound if host metadata is not available to user
        """
        self._enforce(req, 'get_host')
        host_meta = self.get_host_meta_or_404(req, id)
        return {'host_meta': host_meta}

    def detail(self, req):
        """
        Returns detailed information for all available nodes

        :param req: The WSGI/Webob Request object
        :retval The response body is a mapping of the following form::

            {'nodes': [
                {'id': <ID>,
                 'name': <NAME>,
                 'description': <DESCRIPTION>,
                 'created_at': <TIMESTAMP>,
                 'updated_at': <TIMESTAMP>,
                 'deleted_at': <TIMESTAMP>|<NONE>,}, ...
            ]}
        """
        self._enforce(req, 'get_hosts')
        params = self._get_query_params(req)
        try:
            nodes = registry.get_hosts_detail(req.context, **params)
        except exception.Invalid as e:
            raise HTTPBadRequest(explanation=e.msg, request=req)
        return dict(nodes=nodes)

    def _compute_hugepage_memory(self, hugepages, memory, hugepagesize='1G'):
        hugepage_memory = 0
        if hugepagesize == '2M':
            hugepage_memory = 2*1024*int(hugepages)
        if hugepagesize == '1G':
            hugepage_memory = 1*1024*1024*int(hugepages)
        if hugepage_memory > memory:
                    msg = "The memory hugepages used is bigger than total memory."
                    raise HTTPBadRequest(explanation=msg)

    def _host_with_no_pxe_info_in_db(self, host_interfaces):
        input_host_pxe_info = self._count_host_pxe_info(host_interfaces)
        if not input_host_pxe_info:
            return True

    def _host_with_bad_pxe_info_in_params(self, host_meta):
        input_host_pxe_info = self._count_host_pxe_info(host_meta['interfaces'])
        # In default,we think there is only one pxe interface.
        if not input_host_pxe_info:
            LOG.info("<<<The host %s don't have pxe interface.>>>"
                     % host_meta.get('name', None))
            return True
        # If it not only the exception will be raise.
        if len(input_host_pxe_info) > 1:
            msg = ("There are more than one pxe nics among the same host,"
                   "it isn't allowed.")
            raise HTTPBadRequest(explanation=msg)

    def _count_host_pxe_info(self, interfaces):
        interfaces = eval(interfaces)
        input_host_pxe_info = [interface
                               for interface in interfaces
                               if interface.get('is_deployment', None) == "True" or interface.get('is_deployment', None) == "true"
                               or interface.get('is_deployment', None) == 1]
        return input_host_pxe_info

    def _update_networks_phyname(self, req, interface, cluster_id):
        phyname_networks = {}
        cluster_networks = registry.get_networks_detail(req.context, cluster_id)
        for assigned_network in list(interface['assigned_networks']):
            network_info_list = [network for network in cluster_networks
                            if assigned_network['name'] == network['name']]
            if network_info_list and network_info_list[0]:
                network_info = network_info_list[0]
                phyname_networks[network_info['id']] = \
                    [network_info['name'], interface['name']]
            else:
                msg = "can't find network named '%s' in cluster '%s'" % (assigned_network['name'], cluster_id)
                LOG.error(msg)
                raise HTTPBadRequest(explanation=msg,
                                        request=req,
                                        content_type="text/plain")

        # by cluster id and network_name search interface table
        registry.update_phyname_of_network(req.context, phyname_networks)

    def _verify_interface_in_same_host(self, interfaces, id = None):
        """
        Verify interface in the input host.
        :param interface: host interface info
        :return:
        """
        # verify interface among the input host
        interfaces = eval(interfaces)
        same_mac_list = [interface1['name']
                         for interface1 in interfaces for interface2 in interfaces
                         if interface1.get('name', None) and interface1.get('mac', None) and
                            interface2.get('name', None) and interface2.get('mac', None) and
                            interface1.get('type', None) and interface2.get('type', None) and
                         interface1['name'] != interface2['name'] and interface1['mac'] == interface2['mac']
                         and interface1['type'] != "bond" and interface2['type'] != "bond"]
        # Notice:If interface with same 'mac' is illegal,we need delete code #1,and raise exception in 'if' block.
        # This code block is just verify for early warning.
        if same_mac_list:
            msg = "%s%s" % ("" if not id else "Host id:%s." % id,
                            "The nic name of interface [%s] with same mac,please check!" %
                            ",".join(same_mac_list))
            LOG.warn(msg)

        # 1-----------------------------------------------------------------
        # if interface with same 'pci', raise exception
        same_pci_list = [interface1['name']
                         for interface1 in interfaces for interface2 in interfaces
                         if interface1.get('name', None) and interface1.get('pci', None) and
                             interface2.get('name', None) and interface2.get('pci', None) and
                            interface1.get('type', None) and interface2.get('type', None) and
                         interface1['name'] != interface2['name'] and interface1['pci'] == interface2['pci']
                         and interface1['type'] != "bond" and interface2['type'] != "bond"]

        if same_pci_list:
            msg = "The nic name of interface [%s] with same pci,please check!" % ",".join(same_pci_list)
            raise HTTPForbidden(explanation = msg)
        # 1-----------------------------------------------------------------

    def _verify_interface_among_hosts(self, req, host_meta):
        """
        Verify interface among the hosts in cluster
        :param req:
        :param cluster_id:
        :param host_meta:
        :return:True,host already update False,host need add
        """
        # If true, the host need update, not add and update is successful.
        self._verify_interface_in_same_host(host_meta['interfaces'])

        # host pxe interface info
        input_host_pxe_info = self._count_host_pxe_info(host_meta['interfaces'])
        # verify interface between exist host and input host in cluster
        list_params = {
            'sort_key': u'name',
            'sort_dir': u'asc'}
        all_hosts = registry.get_hosts_detail(req.context, **list_params)
        exist_nodes = []
        for id in [host['id'] for host in all_hosts]:
            host_meta_list = registry.get_host_metadata(req.context, id)
            exist_nodes.append(host_meta_list)
        if input_host_pxe_info:
            input_host_pxe_info = input_host_pxe_info[0]
            for exist_node in exist_nodes:
                id = exist_node.get('id', None)
                exist_node_info = self.get_host(req, id).get('host_meta', None)
                if not exist_node_info.get('interfaces', None):
                    continue

                for interface in exist_node_info['interfaces']:
                    if interface.get('mac', None) != input_host_pxe_info.get('mac', None) or \
                                    interface.get('type', None) == "bond":
                        continue
                    if exist_node.get('dmi_uuid', None) != host_meta.get('dmi_uuid', None):
                        msg = "The 'mac' of host interface is exist in db, but 'dmi_uuid' is different." \
                              "We think you want update the host, but the host can't find."
                        raise HTTPForbidden(explanation=msg)
                    return id

    def _get_swap_lv_size_m(self, memory_size_m):
        if memory_size_m <= 4096:
            swap_lv_size_m = 4096
        elif memory_size_m <= 16384:
            swap_lv_size_m = 8192
        elif memory_size_m <= 65536:
            swap_lv_size_m = 32768
        else:
            swap_lv_size_m = 65536
        return swap_lv_size_m
    
    @utils.mutating
    def update_host(self, req, id, host_meta):
        """
        Updates an existing host with the registry.

        :param request: The WSGI/Webob Request object
        :param id: The opaque image identifier

        :retval Returns the updated image information as a mapping
        """
        self._enforce(req, 'update_host')
        orig_host_meta = self.get_host_meta_or_404(req, id)
        # Do not allow any updates on a deleted image.
        # Fix for LP Bug #1060930
        if orig_host_meta['deleted']:
            msg = _("Forbidden to update deleted host.")
            raise HTTPForbidden(explanation=msg,
                                request=req,
                                content_type="text/plain")
        if host_meta.has_key('interfaces'):
            for interface_param in eval(host_meta['interfaces']):
                if not interface_param.get('pci', None) and \
                                interface_param.get('type', None) != 'bond':
                    msg = "The Interface need a non-null pci"
                    raise HTTPBadRequest(explanation=msg,
                                         request=req,
                                         content_type="text/plain")

                if interface_param.has_key('vswitch_type') and interface_param['vswitch_type'] != '' and interface_param['vswitch_type'] not in ML2_TYPE:
                    msg = "vswitch_type %s is not supported" % interface_param['vswitch_type']
                    raise HTTPBadRequest(explanation=msg, request=req,
                                content_type="text/plain")
            if orig_host_meta.get('interfaces', None):
                interfaces_db = orig_host_meta['interfaces']
                interfaces_param = eval(host_meta['interfaces'])
                interfaces_db_ether = [interface_db for interface_db in
                                       interfaces_db if interface_db.get('type', None) != 'bond']
                interfaces_param_ether = [interface_param for interface_param in
                                       interfaces_param if interface_param.get('type', None) != 'bond']
                if len(interfaces_param) < len(interfaces_db_ether):
                    msg = "Forbidden to update part of interfaces"
                    raise HTTPForbidden(explanation=msg)
                pci_count = 0
                for interface_db in interfaces_db:
                    if interface_db.get('type', None) != 'bond':
                        for interface_param in interfaces_param_ether:
                            if interface_param['pci'] == interface_db['pci']:
                                pci_count += 1
                                if interface_param['mac'] != interface_db['mac']:
                                    msg = "Forbidden to modify mac of " \
                                          "interface with pci %s" % interface_db['pci']
                                    raise HTTPForbidden(explanation=msg)
                                if interface_param['type'] != interface_db['type']:
                                    msg = "Forbidden to modify type of " \
                                          "interface with pci %s" % interface_db['pci']
                                    raise HTTPForbidden(explanation=msg)
                if pci_count != len(interfaces_db_ether):
                    msg = "Forbidden to modify pci of interface"
                    raise HTTPForbidden(explanation=msg)

        if host_meta.has_key('cluster'):
            self.get_cluster_meta_or_404(req, host_meta['cluster'])
            if host_meta.has_key('cluster'):
                if orig_host_meta['status'] == 'in-cluster':
                    host_cluster = registry.get_host_clusters(req.context, id)
                    if host_meta['cluster'] != host_cluster[0]['cluster_id']:
                        msg = _("Forbidden to add host %s with status "
                                "'in-cluster' in another cluster") % id
                        raise HTTPForbidden(explanation=msg)
    
        if (host_meta.has_key('resource_type') and
            host_meta['resource_type'] not in self.support_resource_type):
            msg = "resource type is not supported, please use it in %s" % self.support_resource_type
            raise HTTPNotFound(msg)

        if host_meta.get('os_status',None) != 'init' and orig_host_meta.get('os_status',None) == 'active':
            if host_meta.get('root_disk',None) and host_meta['root_disk'] != orig_host_meta['root_disk']:
                msg = _("Forbidden to update root_disk of %s when os_status is active if "
                        "you don't want to install os") % host_meta['name']
                raise HTTPForbidden(explanation=msg)
            else:
                host_meta['root_disk'] = orig_host_meta['root_disk']
        else:
            if host_meta.get('root_disk',None):
                root_disk = host_meta['root_disk']
            elif orig_host_meta.get('root_disk',None):
                root_disk = str(orig_host_meta['root_disk'])
            else:
                host_meta['root_disk'] = 'sda'
                root_disk = host_meta['root_disk']
            if not orig_host_meta.get('disks',None):
                    msg = "there is no disks in %s" %orig_host_meta['id']
                    raise HTTPNotFound(msg)
            if root_disk not in orig_host_meta['disks'].keys():
                msg = "There is no disk named %s" % root_disk
                raise HTTPBadRequest(explanation=msg,
                                    request=req,
                                    content_type="text/plain")

        if host_meta.get('os_status',None) != 'init' and orig_host_meta.get('os_status',None) == 'active':
            if host_meta.get('root_lv_size',None) and int(host_meta['root_lv_size']) != orig_host_meta['root_lv_size']:
                msg = _("Forbidden to update root_lv_size of %s when os_status is active if "
                        "you don't want to install os") % host_meta['name']
                raise HTTPForbidden(explanation=msg)
            else:
                host_meta['root_lv_size'] = str(orig_host_meta['root_lv_size'])
        else:
            if host_meta.get('root_lv_size',None):
                root_lv_size = host_meta['root_lv_size']
            elif orig_host_meta.get('root_lv_size',None):
                root_lv_size = str(orig_host_meta['root_lv_size'])
            else:
                host_meta['root_lv_size'] = '51200'
                root_lv_size = host_meta['root_lv_size']
            if not orig_host_meta.get('disks',None):
                    msg = "there is no disks in %s" %orig_host_meta['id']
                    raise HTTPNotFound(msg)
            if root_lv_size.isdigit():
                root_lv_size=int(root_lv_size)
                root_disk_storage_size_b_str = str(orig_host_meta['disks']['%s' %root_disk]['size'])
                root_disk_storage_size_b_int = int(root_disk_storage_size_b_str.strip().split()[0])
                root_disk_storage_size_m = root_disk_storage_size_b_int//(1024*1024)
                boot_partition_m = 400
                redundant_partiton_m = 600
                free_root_disk_storage_size_m = root_disk_storage_size_m - boot_partition_m - redundant_partiton_m
                if (root_lv_size/4)*4 > free_root_disk_storage_size_m:
                    msg = "root_lv_size of %s is larger than the free_root_disk_storage_size."%orig_host_meta['id']
                    raise HTTPForbidden(explanation=msg,
                                        request=req,
                                        content_type="text/plain")
                if (root_lv_size/4)*4 < 51200:
                    msg = "root_lv_size of %s is too small ,it must be larger than 51200M."%orig_host_meta['id']
                    raise HTTPForbidden(explanation=msg,
                                        request=req,
                                        content_type="text/plain")
            else:
                msg = (_("root_lv_size of %s is wrong,please input a number and it must be positive number") %orig_host_meta['id'])
                raise HTTPForbidden(explanation=msg,
                                    request=req,
                                    content_type="text/plain")

        if host_meta.get('os_status',None) != 'init' and orig_host_meta.get('os_status',None) == 'active':
            if host_meta.get('swap_lv_size',None) and int(host_meta['swap_lv_size']) != orig_host_meta['swap_lv_size']:
                msg = _("Forbidden to update swap_lv_size of %s when os_status is active if "
                        "you don't want to install os") % host_meta['name']
                raise HTTPForbidden(explanation=msg)
            else:
                host_meta['swap_lv_size'] = str(orig_host_meta['swap_lv_size'])
        else:
            if host_meta.get('swap_lv_size',None):
                swap_lv_size = host_meta['swap_lv_size']
            elif orig_host_meta.get('swap_lv_size',None):
                swap_lv_size = str(orig_host_meta['swap_lv_size'])
            else:
                if not orig_host_meta.get('memory',None):
                    msg = "there is no memory in %s" %orig_host_meta['id']
                    raise HTTPNotFound(msg)
                memory_size_b_str = str(orig_host_meta['memory']['total'])
                memory_size_b_int = int(memory_size_b_str.strip().split()[0])
                memory_size_m = memory_size_b_int//1024
                swap_lv_size_m = self._get_swap_lv_size_m(memory_size_m)
                host_meta['swap_lv_size'] = str(swap_lv_size_m)
                swap_lv_size = host_meta['swap_lv_size']
            if swap_lv_size.isdigit():
                swap_lv_size=int(swap_lv_size)
                disk_storage_size_b = 0
                for key in orig_host_meta['disks']:
                    stroage_size_str = orig_host_meta['disks'][key]['size']
                    stroage_size_b_int = int(stroage_size_str.strip().split()[0])
                    disk_storage_size_b = disk_storage_size_b + stroage_size_b_int
                disk_storage_size_m = disk_storage_size_b/(1024*1024)
                boot_partition_m = 400
                redundant_partiton_m = 600
                if host_meta.get('role',None):
                    host_role_names = eval(host_meta['role'])
                elif orig_host_meta.get('role',None):
                    host_role_names = orig_host_meta['role']
                else:
                    host_role_names = None
                if host_role_names:
                    roles_of_host=[]
                    params = self._get_query_params(req)
                    role_lists = registry.get_roles_detail(req.context, **params)
                    for host_role_name in host_role_names:
                        for role in role_lists:
                            if host_role_name == role['name'] and role['type'] == 'default':
                                roles_of_host.append(role)
                    db_lv_size = 0
                    nova_lv_size = 0
                    glance_lv_size = 0
                    for role_of_host in roles_of_host:
                        if role_of_host['name'] == 'CONTROLLER_HA':
                            if role_of_host.get('glance_lv_size',None):
                                glance_lv_size = role_of_host['glance_lv_size']
                            if role_of_host.get('db_lv_size',None):
                                db_lv_size = role_of_host['db_lv_size']
                        if role_of_host['name'] == 'COMPUTER':
                            nova_lv_size = role_of_host['nova_lv_size']
                    free_disk_storage_size_m = disk_storage_size_m - boot_partition_m - redundant_partiton_m - \
                            (root_lv_size/4)*4 - (glance_lv_size/4)*4- (nova_lv_size/4)*4- (db_lv_size/4)*4
                else:
                    free_disk_storage_size_m = disk_storage_size_m - boot_partition_m - \
                                               redundant_partiton_m - (root_lv_size/4)*4
                if (swap_lv_size/4)*4 > free_disk_storage_size_m:
                    msg = "the sum of swap_lv_size and glance_lv_size and nova_lv_size and db_lv_size of %s is larger " \
                          "than the free_disk_storage_size."%orig_host_meta['id']
                    raise HTTPForbidden(explanation=msg,
                                        request=req,
                                        content_type="text/plain")
                if (swap_lv_size/4)*4 < 2000:
                    msg = "swap_lv_size of %s is too small ,it must be larger than 2000M."%orig_host_meta['id']
                    raise HTTPForbidden(explanation=msg,
                                        request=req,
                                        content_type="text/plain")
            else:
                msg = (_("swap_lv_size of %s is wrong,please input a number and it must be positive number") %orig_host_meta['id'])
                raise HTTPForbidden(explanation=msg,
                                    request=req,
                                    content_type="text/plain")

            
        if host_meta.get('os_status',None) != 'init' and orig_host_meta.get('os_status',None) == 'active':
            if host_meta.get('root_pwd',None) and host_meta['root_pwd'] != orig_host_meta['root_pwd']:
                msg = _("Forbidden to update root_pwd of %s when os_status is active if "
                        "you don't want to install os") % host_meta['name']
                raise HTTPForbidden(explanation=msg)
            else:
                host_meta['root_pwd'] = orig_host_meta['root_pwd']
        else:
            if not host_meta.get('root_pwd',None) and not orig_host_meta.get('root_pwd',None):
                host_meta['root_pwd'] = 'ossdbg1'

        if host_meta.get('os_status',None) != 'init' and orig_host_meta.get('os_status',None) == 'active':
            if host_meta.get('isolcpus',None) and host_meta['isolcpus'] != orig_host_meta['isolcpus']:
                msg = _("Forbidden to update isolcpus of %s when os_status is active if "
                        "you don't want to install os") % host_meta['name']
                raise HTTPForbidden(explanation=msg)
            else:
                host_meta['isolcpus'] = orig_host_meta['isolcpus']
        else:
            if host_meta.get('isolcpus',None):
                isolcpus = host_meta['isolcpus']
            elif orig_host_meta.get('isolcpus',None):
                isolcpus = orig_host_meta['isolcpus']
            else:
                host_meta['isolcpus'] = None
                isolcpus = host_meta['isolcpus']
            if not orig_host_meta.get('cpu',None):
                    msg = "there is no cpu in %s" %orig_host_meta['id']
                    raise HTTPNotFound(msg)
            cpu_num = orig_host_meta['cpu']['total']
            if isolcpus:
                isolcpus_lists = [value.split('-') for value in isolcpus.split(',')]
                isolcpus_list = []
                for value in isolcpus_lists:
                    isolcpus_list = isolcpus_list + value
                for value in isolcpus_list:
                    if int(value)<0 or int(value)>cpu_num -1:
                        msg = "isolcpus number must be lager than 0 and less than %d" %(cpu_num-1)
                        raise HTTPForbidden(explanation=msg,
                                        request=req,
                                        content_type="text/plain")

        if host_meta.has_key('role'):
            role_id_list = []
            if host_meta.has_key('cluster'):
                params = self._get_query_params(req)
                role_list = registry.get_roles_detail(req.context, **params)
                host_roles = list()
                for role_name in role_list:
                    if role_name['cluster_id'] == host_meta['cluster']:
                        host_roles = list(eval(host_meta['role']))
                        for host_role in host_roles:
                            if role_name['name'] == host_role:
                                role_id_list.append(role_name['id'])
                                continue
                if len(role_id_list) != len(host_roles) and host_meta['role'] != u"[u'']":
                    msg = "The role of params %s is not exist, please use the right name" % host_roles
                    raise HTTPNotFound(msg)
                host_meta['role'] = role_id_list
            else:
                msg = "cluster params is none"
                raise HTTPNotFound(msg)

        if host_meta.has_key('interfaces'):
            if self._host_with_bad_pxe_info_in_params(host_meta):
                msg = _('The parameter interfaces of %s is wrong, there is no interface for pxe.') % id
                #raise HTTPBadRequest(explanation=msg)
            else:
                host_meta_interfaces = list(eval(host_meta['interfaces']))
                ether_nic_names_list = list()
                bond_nic_names_list = list()
                bond_slaves_lists = list()
                interface_num = 0
                assigned_networks_of_interfaces = []
                for interface in host_meta_interfaces:
                    if interface.get('name', None):
                        if interface.has_key('type') and interface['type'] == 'bond':
                            bond_nic_names_list.append(interface['name'])
                            slave_list = []
                            if interface.get('slaves', None):
                                bond_slaves_lists.append(interface['slaves'])
                            elif interface.get('slave1', None) and interface.get('slave2', None):
                                slave_list.append(interface['slave1'])
                                slave_list.append(interface['slave2'])
                                bond_slaves_lists.append(slave_list)
                            else:
                                msg = (_("Slaves parameter can not be None when nic type was bond."))
                                LOG.error(msg)
                                raise HTTPForbidden(msg)
                        else:  # type == ether or interface without type field
                            ether_nic_names_list.append(interface['name'])
                    else:
                        msg = (_("Nic name can not be None."))
                        LOG.error(msg)
                        raise HTTPForbidden(msg)
                    if interface.has_key('is_deployment'):
                        if interface['is_deployment'] == "True" or interface['is_deployment'] == True:
                            interface['is_deployment'] = 1
                        else:
                            interface['is_deployment'] = 0

                    if (interface.has_key('assigned_networks') and 
                            interface['assigned_networks'] != [''] and 
                            interface['assigned_networks']):
                        clusters = registry.get_clusters_detail(req.context)
                        orig_cluster_name = orig_host_meta.get('cluster', None)
                        orig_cluster_id = None
                        for cluster in clusters:
                            if cluster['name'] == orig_cluster_name:
                                orig_cluster_id = cluster['id']
                        cluster_id = host_meta.get('cluster', orig_cluster_id)
                        if cluster_id:
                            LOG.info("interface['assigned_networks']: %s" % interface['assigned_networks'])
                            assigned_networks_of_one_interface = self.\
                                _check_assigned_networks(req,
                                                         cluster_id,
                                                         interface['assigned_'
                                                                   'networks'])
                            self._update_networks_phyname(req, interface, cluster_id)
                            host_meta['cluster'] = cluster_id
                        else:
                            msg = "cluster must be given first when network plane is allocated"
                            LOG.error(msg)
                            raise HTTPBadRequest(explanation=msg,
                                                 request=req,
                                                 content_type="text/plain")
                        assigned_networks_of_interfaces.\
                            append(assigned_networks_of_one_interface)
                    else:
                        assigned_networks_of_interfaces.\
                            append([])
                    interface_num += 1
                self._compare_assigned_networks_between_interfaces\
                    (interface_num, assigned_networks_of_interfaces)

                # check bond slaves validity
                self.check_bond_slaves_validity(bond_slaves_lists, ether_nic_names_list)
                nic_name_list = ether_nic_names_list + bond_nic_names_list
                if len(set(nic_name_list)) != len(nic_name_list):
                    msg = (_("Nic name must be unique."))
                    LOG.error(msg)
                    raise HTTPForbidden(msg)
        else:
            if host_meta.has_key('cluster'):
                host_interfaces = orig_host_meta.get('interfaces', None)
                if host_interfaces:
                    if host_meta.has_key('os_status'):
                        if host_meta['os_status'] != 'active':
                            if self._host_with_no_pxe_info_in_db(str(host_interfaces)):
                                msg = _("The host has more than one dhcp "
                                        "server, please choose one interface "
                                        "for deployment")
                                raise HTTPServerError(explanation=msg)
                    else:
                        if orig_host_meta.get('os_status', None) != 'active':
                            if self._host_with_no_pxe_info_in_db(str(host_interfaces)):
                                msg = _("There is no nic for deployment, "
                                        "please choose one interface to set "
                                        "it's 'is_deployment' True")
                                raise HTTPServerError(explanation=msg)

        if host_meta.has_key('os_status'):
            if host_meta['os_status'] not in ['init', 'installing', 'active', 'failed', 'none']:
                msg = "os_status is not valid."
                raise HTTPNotFound(msg)
            if host_meta['os_status'] == 'init':
                if orig_host_meta.get('interfaces', None):
                    macs = [interface['mac'] for interface in orig_host_meta['interfaces'] 
                               if interface['mac']]
                    for mac in macs:
                        delete_host_discovery_info = 'pxe_os_install_clean ' + mac
                        subprocess.call(delete_host_discovery_info,
                                        shell=True,
                                        stdout=open('/dev/null', 'w'),
                                        stderr=subprocess.STDOUT)
                if (not host_meta.has_key('role') and 
                    orig_host_meta.has_key('status') and
                    orig_host_meta['status'] == 'with-role' and
                    orig_host_meta['os_status'] != 'init'):
                    host_meta['role'] = []
                if not host_meta.has_key('os_progress'):
                    host_meta['os_progress'] = 0
                if not host_meta.has_key('messages'):
                    host_meta['messages'] = ''          
            
        if ((host_meta.has_key('ipmi_addr') and host_meta['ipmi_addr']) 
            or orig_host_meta['ipmi_addr']):
            if not host_meta.has_key('ipmi_user') and not orig_host_meta['ipmi_user']:
                host_meta['ipmi_user'] = 'zteroot'
            if not host_meta.has_key('ipmi_passwd') and not orig_host_meta['ipmi_passwd']:
                host_meta['ipmi_passwd'] = 'superuser'

        if host_meta.get('os_status',None) != 'init' and orig_host_meta.get('os_status',None) == 'active':
            if host_meta.get('hugepages',None) and int(host_meta['hugepages']) != orig_host_meta['hugepages']:
                msg = _("Forbidden to update hugepages of %s when os_status is active if "
                        "you don't want to install os") % host_meta['name']
                raise HTTPForbidden(explanation=msg)
            else:
                host_meta['hugepages'] = str(orig_host_meta['hugepages'])
        else:
            if host_meta.has_key('hugepages'):
                if not orig_host_meta.get('memory', {}).get('total', None):
                    msg = "The host %s has no memory" % id
                    raise HTTPNotFound(explanation=msg)
                memory = orig_host_meta.get('memory', {}).get('total', None)
                if host_meta['hugepages'] is None:
                    host_meta['hugepages'] = 0
                if int(host_meta['hugepages']) < 0:
                    msg = "The parameter hugepages must be zero or positive integer."
                    raise HTTPBadRequest(explanation=msg)
                if not host_meta.has_key('hugepagesize') and \
                        orig_host_meta.get('hugepagesize', None):
                    self._compute_hugepage_memory(host_meta['hugepages'],
                                                  int(memory.strip().split(' ')[0]),
                                                  orig_host_meta['hugepagesize'])
                if not host_meta.has_key('hugepagesize') and \
                        not orig_host_meta.get('hugepagesize', None):
                    self._compute_hugepage_memory(host_meta['hugepages'],
                                                  int(memory.strip().split(' ')[0]))

        if host_meta.get('os_status',None) != 'init' and orig_host_meta.get('os_status',None) == 'active':
            if host_meta.get('hugepagesize',None) and host_meta['hugepagesize'] != orig_host_meta['hugepagesize']:
                msg = _("Forbidden to update hugepagesize of %s when os_status is active if "
                        "you don't want to install os") % host_meta['name']
                raise HTTPForbidden(explanation=msg)
            else:
                host_meta['hugepagesize'] = orig_host_meta['hugepagesize']
        else:
            if host_meta.has_key('hugepagesize'):
                if not orig_host_meta.get('memory', {}).get('total', None):
                    msg = "The host %s has no memory" % id
                    raise HTTPNotFound(explanation=msg)
                memory = orig_host_meta.get('memory', {}).get('total', None)
                if host_meta['hugepagesize'] is None:
                    host_meta['hugepagesize'] = '1G'
                elif host_meta['hugepagesize'] != '2m' and \
                                host_meta['hugepagesize'] != '2M' and \
                                host_meta['hugepagesize'] != '1g' and \
                                host_meta['hugepagesize'] != '1G':
                    msg = "The value 0f parameter hugepagesize is not supported."
                    raise HTTPBadRequest(explanation=msg)
                if host_meta['hugepagesize'] == '2m':
                    host_meta['hugepagesize'] = '2M'
                if host_meta['hugepagesize'] == '1g':
                    host_meta['hugepagesize'] = '1G'
                if host_meta['hugepagesize'] == '2M' and \
                                int(host_meta['hugepagesize'][0])*1024 > \
                                int(memory.strip().split(' ')[0]):
                    msg = "The host %s forbid to use hugepage because it's " \
                          "memory is too small" % id
                    raise HTTPForbidden(explanation=msg)
                if host_meta['hugepagesize'] == '1G' and \
                                int(host_meta['hugepagesize'][0])*1024*1024 > \
                                int(memory.strip().split(' ')[0]):
                    msg = "The hugepagesize is too big, you can choose 2M " \
                          "for a try."
                    raise HTTPBadRequest(explanation=msg)
                if host_meta.has_key('hugepages'):
                    self._compute_hugepage_memory(host_meta['hugepages'],
                                                  int(memory.strip().split(' ')[0]),
                                                  host_meta['hugepagesize'])
                if not host_meta.has_key('hugepages') and orig_host_meta.get('hugepages', None):
                    self._compute_hugepage_memory(orig_host_meta['hugepages'],
                                                  int(memory.strip().split(' ')[0]),
                                                  host_meta['hugepagesize'])

        if host_meta.get('os_status',None) != 'init' and orig_host_meta.get('os_status',None) == 'active':
            if host_meta.get('os_version',None) and host_meta['os_version'] != orig_host_meta['os_version_file']:
                msg = _("Forbidden to update os_version of %s when os_status is active if "
                        "you don't want to install os") % host_meta['name']
                raise HTTPForbidden(explanation=msg)

        try:
            host_meta = registry.update_host_metadata(req.context,
                                                      id,
                                                      host_meta)

        except exception.Invalid as e:
            msg = (_("Failed to update host metadata. Got error: %s") %
                   utils.exception_to_str(e))
            LOG.error(msg)
            raise HTTPBadRequest(explanation=msg,
                                 request=req,
                                 content_type="text/plain")
        except exception.NotFound as e:
            msg = (_("Failed to find host to update: %s") %
                   utils.exception_to_str(e))
            LOG.error(msg)
            raise HTTPNotFound(explanation=msg,
                               request=req,
                               content_type="text/plain")
        except exception.Forbidden as e:
            msg = (_("Forbidden to update host: %s") %
                   utils.exception_to_str(e))
            LOG.error(msg)
            raise HTTPForbidden(explanation=msg,
                                request=req,
                                content_type="text/plain")
        except (exception.Conflict, exception.Duplicate) as e:
            LOG.error(utils.exception_to_str(e))
            raise HTTPConflict(body=_('Host operation conflicts'),
                               request=req,
                               content_type='text/plain')
        else:
            self.notifier.info('host.update', host_meta)

        return {'host_meta': host_meta}
        
        
    def update_progress_to_db(self, req, update_info, discover_host_meta):
        discover= {}
        discover['status'] = update_info['status']
        discover['message'] = update_info['message']
        if update_info.get('host_id'):
            discover['host_id'] = update_info['host_id']
        LOG.info("discover:%s", discover)
        registry.update_discover_host_metadata(req.context, discover_host_meta['id'], discover)
        
    def thread_bin(self,req,discover_host_meta):
        cmd = 'mkdir -p /var/log/daisy/discover_host/'
        daisy_cmn.subprocess_call(cmd)
        if not discover_host_meta['passwd']:
            msg = "the passwd of ip %s is none."%discover_host_meta['ip']
            LOG.error(msg)
            raise HTTPForbidden(msg)
        var_log_path = "/var/log/daisy/discover_host/%s_discovery_host.log" % discover_host_meta['ip']
        with open(var_log_path, "w+") as fp:
            try:
                trustme_result = subprocess.check_output(
                    '/var/lib/daisy/tecs/trustme.sh %s %s' % (discover_host_meta['ip'],discover_host_meta['passwd']),
                    shell=True, stderr=subprocess.STDOUT)
                if 'Permission denied' in trustme_result:  #when passwd was wrong
                    update_info = {}
                    update_info['status'] = 'DISCOVERY_FAILED'
                    update_info['message'] = "Passwd was wrong, do trustme.sh %s failed!" % discover_host_meta['ip']
                    self.update_progress_to_db(req, update_info, discover_host_meta)
                    msg = (_("Do trustme.sh %s failed!" % discover_host_meta['ip']))
                    LOG.warn(_(msg))
                    fp.write(msg)
                elif 'is unreachable' in trustme_result: #when host ip was unreachable
                    update_info = {}
                    update_info['status'] = 'DISCOVERY_FAILED'
                    update_info['message'] = "Host ip was unreachable, do trustme.sh %s failed!" % discover_host_meta['ip']
                    self.update_progress_to_db(req,update_info, discover_host_meta)
                    msg = (_("Do trustme.sh %s failed!" % discover_host_meta['ip']))
                    LOG.warn(_(msg))
            except subprocess.CalledProcessError as e:
                update_info = {}
                update_info['status'] = 'DISCOVERY_FAILED'
                msg = "discover host for %s failed! raise CalledProcessError when execute trustme.sh." % discover_host_meta['ip']
                update_info['message'] = msg
                self.update_progress_to_db(req,update_info, discover_host_meta)
                LOG.error(_(msg))
                fp.write(e.output.strip())
                return
            except:
                update_info = {}
                update_info['status'] = 'DISCOVERY_FAILED'
                update_info['message'] = "discover host for %s failed!" % discover_host_meta['ip']
                self.update_progress_to_db(req,update_info, discover_host_meta)
                LOG.error(_("discover host for %s failed!" % discover_host_meta['ip']))
                fp.write("discover host for %s failed!" % discover_host_meta['ip'])
                return

            try:
                cmd = 'clush -S -b -w %s "rm -rf /home/daisy/discover_host"' % (discover_host_meta['ip'],)
                daisy_cmn.subprocess_call(cmd,fp)
                cmd = 'clush -S -w %s "mkdir -p /home/daisy/discover_host"' % (discover_host_meta['ip'],)
                daisy_cmn.subprocess_call(cmd,fp)
                cmd = 'clush -S -w %s "chmod 777 /home/daisy/discover_host"' % (discover_host_meta['ip'],)
                daisy_cmn.subprocess_call(cmd,fp)
            except subprocess.CalledProcessError as e:
                update_info = {}
                update_info['status'] = 'DISCOVERY_FAILED'
                msg = "raise CalledProcessError when execute cmd for host %s." % discover_host_meta['ip']
                update_info['message'] = msg
                self.update_progress_to_db(req,update_info, discover_host_meta)
                LOG.error(_(msg))
                fp.write(e.output.strip())
                return

            try:
                scp_sh_and_rpm_result = subprocess.check_output(
                    'clush -S -w %s -c /var/lib/daisy/tecs/getnodeinfo.sh /var/lib/daisy/tecs/jq-1.3-2.el7.x86_64.rpm --dest=/home/daisy/discover_host' % (discover_host_meta['ip'],),
                    shell=True, stderr=subprocess.STDOUT)
            except subprocess.CalledProcessError as e:
                update_info = {}
                update_info['status'] = 'DISCOVERY_FAILED'
                update_info['message'] = "scp getnodeinfo.sh and jq-1.3-2.el7.x86_64.rpm for %s failed!" % discover_host_meta['ip']
                self.update_progress_to_db(req, update_info, discover_host_meta)
                LOG.error(_("scp getnodeinfo.sh and jq-1.3-2.el7.x86_64.rpm for %s failed!" % discover_host_meta['ip']))
                fp.write(e.output.strip())
                return
                
            try:
                rpm_install_result = subprocess.check_output(
                    'clush -S -w %s rpm -ivh --force /home/daisy/discover_host/jq-1.3-2.el7.x86_64.rpm' % (discover_host_meta['ip'],),
                    shell=True, stderr=subprocess.STDOUT)
            except subprocess.CalledProcessError as e:
                update_info = {}
                update_info['status'] = 'DISCOVERY_FAILED'
                update_info['message'] = "install jq-1.3-2.el7.x86_64.rpm for %s failed!" % discover_host_meta['ip']
                self.update_progress_to_db(req, update_info, discover_host_meta)
                LOG.error(_("install jq-1.3-2.el7.x86_64.rpm for %s failed!" % discover_host_meta['ip']))
                fp.write(e.output.strip())
                return
            
            try:
                exc_result = subprocess.check_output(
                    'clush -S -w %s /home/daisy/discover_host/getnodeinfo.sh' % (discover_host_meta['ip'],),
                    shell=True, stderr=subprocess.STDOUT)
                if 'Failed connect to' in exc_result:  #when openstack-ironic-discoverd.service has problem
                    update_info = {}
                    update_info['status'] = 'DISCOVERY_FAILED'
                    update_info['message'] = "Do getnodeinfo.sh %s failed!" % discover_host_meta['ip']
                    self.update_progress_to_db(req, update_info, discover_host_meta)
                    msg = (_("Do trustme.sh %s failed!" % discover_host_meta['ip']))
                    LOG.warn(_(msg))
                    fp.write(msg)
                else:
                    update_info = {}
                    update_info['status'] = 'DISCOVERY_SUCCESSFUL'
                    update_info['message'] = "discover host for %s successfully!" % discover_host_meta['ip']
                    mac_info = re.search(r'"mac": ([^,\n]*)', exc_result)
                    mac = eval(mac_info.group(1))
                    filters = {'mac': mac}
                    host_interfaces = registry.get_all_host_interfaces(req.context, filters)
                    if host_interfaces:
                        update_info['host_id'] = host_interfaces[0]['host_id']
                        LOG.info("update_info['host_id']:%s", update_info['host_id'])
                    self.update_progress_to_db(req,update_info, discover_host_meta)
                    LOG.info(_("discover host for %s successfully!" % discover_host_meta['ip']))
                    fp.write(exc_result)

            except subprocess.CalledProcessError as e:
                update_info = {}
                update_info['status'] = 'DISCOVERY_FAILED'
                update_info['message'] = "discover host for %s failed!" % discover_host_meta['ip']
                self.update_progress_to_db(req,update_info, discover_host_meta)
                LOG.error(_("discover host for %s failed!" % discover_host_meta['ip']))
                fp.write(e.output.strip())
                return
            

    @utils.mutating
    def discover_host_bin(self, req, host_meta):
        params={}
        discover_host_meta_list=registry.get_discover_hosts_detail(req.context, **params)
        filters = {}
        host_interfaces = registry.get_all_host_interfaces(req.context, filters)
        existed_host_ip = [host['ip'] for host in host_interfaces]
        LOG.info('existed_host_ip**: %s', existed_host_ip)

        for discover_host in discover_host_meta_list:
            if discover_host['status'] != 'DISCOVERY_SUCCESSFUL':
                update_info = {}
                update_info['status'] = 'DISCOVERING'
                update_info['message'] = 'DISCOVERING'
                update_info['host_id'] = 'None'
                self.update_progress_to_db(req, update_info,  discover_host)
        threads = []
        for discover_host_meta in discover_host_meta_list:
            if discover_host_meta['ip'] in existed_host_ip:
                update_info = {}
                update_info['status'] = 'DISCOVERY_SUCCESSFUL'
                update_info['message'] = "discover host for %s successfully!" % discover_host_meta['ip']
                host_id_list = [host['host_id'] for host in host_interfaces if discover_host_meta['ip'] == host['ip']]
                update_info['host_id'] = host_id_list[0]
                self.update_progress_to_db(req,update_info, discover_host_meta)
                continue
            if discover_host_meta['status'] != 'DISCOVERY_SUCCESSFUL':
                t = threading.Thread(target=self.thread_bin,args=(req,discover_host_meta))
                t.setDaemon(True)
                t.start()
                threads.append(t)
        LOG.info(_("all host discovery threads have started, please waiting...."))

        try:
            for t in threads:
                t.join()
        except:
            LOG.warn(_("Join discover host thread %s failed!" % t))
        
    @utils.mutating
    def discover_host(self, req, host_meta):
        daisy_management_ip=config.get("DEFAULT", "daisy_management_ip")
        if daisy_management_ip:
            cmd = 'dhcp_linenumber=`grep -n "dhcp_ip=" /var/lib/daisy/tecs/getnodeinfo.sh|cut -d ":" -f 1` && sed -i "${dhcp_linenumber}c dhcp_ip=\'%s\'" /var/lib/daisy/tecs/getnodeinfo.sh'% (daisy_management_ip,)
            daisy_cmn.subprocess_call(cmd)
    
        discovery_host_thread = threading.Thread(target=self.discover_host_bin,args=(req, host_meta))
        discovery_host_thread.start()
        return {"status":"begin discover host"}
    
    @utils.mutating
    def add_discover_host(self, req, host_meta):
        """
        Adds a new discover host to Daisy

        :param req: The WSGI/Webob Request object
        :param host_meta: Mapping of metadata about host

        :raises HTTPBadRequest if x-host-name is missing
        """
        self._enforce(req, 'add_discover_host')
        LOG.warn("host_meta: %s" % host_meta)
        if not host_meta.get('ip', None):
            msg = "IP parameter can not be None."
            raise HTTPBadRequest(explanation=msg,
                                 request=req,
                                 content_type="text/plain")
        else:
            discover_hosts_ip = self._get_discover_host_ip(req)
            if host_meta['ip'] in discover_hosts_ip:
                host = self._get_host_by_ip(req, host_meta['ip'])
                if host and host['status'] != 'DISCOVERY_SUCCESSFUL':
                    host_info = {}
                    host_info['ip'] = host_meta.get('ip', host.get('ip'))
                    host_info['passwd'] = host_meta.get('passwd', host.get('passwd'))
                    host_info['user'] =  host_meta.get('user', host.get('user'))
                    host_info['status'] = 'init'
                    host_info['message'] =  'None'
                    host_meta = registry.update_discover_host_metadata(req.context,
                                                      host['id'],
                                                      host_info)
                    return {'host_meta': host_meta}
                else:
                    msg = (_("ip %s already existed and this host has been discovered successfully. " % host_meta['ip']))
                    LOG.error(msg)
                    raise HTTPForbidden(explanation=msg,
                                    request=req,
                                    content_type="text/plain")
            self.validate_ip_format(host_meta['ip'])
            
        if not host_meta.get('user', None):
            host_meta['user'] = 'root'
        
        if not host_meta.get('passwd', None):
            msg = "PASSWD parameter can not be None."
            raise HTTPBadRequest(explanation=msg,
                                 request=req,
                                 content_type="text/plain")
        if not host_meta.get('status', None):
            host_meta['status'] = 'init'

        try:
            discover_host_info = registry.add_discover_host_metadata(req.context, host_meta)
        except exception.Invalid as e:
            raise HTTPBadRequest(explanation=e.msg, request=req)
        return {'host_meta': discover_host_info}
    
    @utils.mutating
    def delete_discover_host(self, req, id):
        """
        Deletes a discover host from Daisy.

        :param req: The WSGI/Webob Request object
        :param image_meta: Mapping of metadata about host

        :raises HTTPBadRequest if x-host-name is missing
        """
        self._enforce(req, 'delete_discover_host')
        try:
            registry.delete_discover_host_metadata(req.context, id)
        except exception.NotFound as e:
            msg = (_("Failed to find host to delete: %s") %
                   utils.exception_to_str(e))
            LOG.error(msg)
            raise HTTPNotFound(explanation=msg,
                               request=req,
                               content_type="text/plain")
        except exception.Forbidden as e:
            msg = (_("Forbidden to delete host: %s") %
                   utils.exception_to_str(e))
            LOG.error(msg)
            raise HTTPForbidden(explanation=msg,
                                request=req,
                                content_type="text/plain")
        except exception.InUseByStore as e:
            msg = (_("Host %(id)s could not be deleted because it is in use: "
                     "%(exc)s") % {"id": id, "exc": utils.exception_to_str(e)})
            LOG.error(msg)
            raise HTTPConflict(explanation=msg,
                               request=req,
                               content_type="text/plain")
        else:
            #self.notifier.info('host.delete', host)
            return Response(body='', status=200)
            
    def detail_discover_host(self, req):
        """
        Returns detailed information for all available nodes

        :param req: The WSGI/Webob Request object
        :retval The response body is a mapping of the following form::

            {'nodes': [
                {'id': <ID>,
                 'name': <NAME>,
                 'description': <DESCRIPTION>,
                 'created_at': <TIMESTAMP>,
                 'updated_at': <TIMESTAMP>,
                 'deleted_at': <TIMESTAMP>|<NONE>,}, ...
            ]}
        """

        self._enforce(req, 'get_discover_hosts')
        params = self._get_query_params(req)
        try:
            nodes = registry.get_discover_hosts_detail(req.context, **params)
        except exception.Invalid as e:
            raise HTTPBadRequest(explanation=e.msg, request=req)

        return dict(nodes=nodes)            
            
    def update_discover_host(self, req, id, host_meta):
        '''
        '''
        self._enforce(req, 'update_discover_host')
        params = {'id': id}
        orig_host_meta = registry.get_discover_host_metadata(req.context, id)
        if host_meta.get('ip', None):
            discover_hosts_ip = self._get_discover_host_ip(req)
            if host_meta['ip'] in discover_hosts_ip:
                host_status = host_meta.get('status', orig_host_meta['status'])
                if host_status == 'DISCOVERY_SUCCESSFUL':
                    msg = (_("Host with ip %s already has been discovered successfully, can not change host ip to %s " % (orig_host_meta['ip'], host_meta['ip'])))
                    LOG.error(msg)
                    raise HTTPForbidden(explanation=msg,
                                request=req,
                                content_type="text/plain")
            self.validate_ip_format(host_meta['ip'])
        if orig_host_meta['ip'] != host_meta['ip']:
            host_meta['status'] = 'init'
        try:
            host_meta = registry.update_discover_host_metadata(req.context,
                                                      id,
                                                      host_meta)

        except exception.Invalid as e:
            msg = (_("Failed to update host metadata. Got error: %s") %
                   utils.exception_to_str(e))
            LOG.error(msg)
            raise HTTPBadRequest(explanation=msg,
                                 request=req,
                                 content_type="text/plain")
        except exception.NotFound as e:
            msg = (_("Failed to find host to update: %s") %
                   utils.exception_to_str(e))
            LOG.error(msg)
            raise HTTPNotFound(explanation=msg,
                               request=req,
                               content_type="text/plain")
        except exception.Forbidden as e:
            msg = (_("Forbidden to update host: %s") %
                   utils.exception_to_str(e))
            LOG.error(msg)
            raise HTTPForbidden(explanation=msg,
                                request=req,
                                content_type="text/plain")
        except (exception.Conflict, exception.Duplicate) as e:
            LOG.error(utils.exception_to_str(e))
            raise HTTPConflict(body=_('Host operation conflicts'),
                               request=req,
                               content_type='text/plain')
        else:
            self.notifier.info('host.update', host_meta)

        return {'host_meta': host_meta}

    def _get_discover_host_ip(self, req):
        params= {}
        hosts_ip = list()
        discover_hosts = registry.get_discover_hosts_detail(req.context, **params)
        for host in discover_hosts:
            if host.get('ip', None):
                hosts_ip.append(host['ip'])
        return hosts_ip

    def _get_host_by_ip(self, req, host_ip):
        params= {}
        discover_hosts = registry.get_discover_hosts_detail(req.context, **params)
        LOG.info("%s" % discover_hosts)
        for host in discover_hosts:
            if host.get('ip') == host_ip:
                return host
        return

    def get_discover_host_detail(self, req, discover_host_id):
        '''
        '''
        try:
            host_meta = registry.get_discover_host_metadata(req.context, discover_host_id)
        except exception.Invalid as e:
            msg = (_("Failed to update host metadata. Got error: %s") %
                   utils.exception_to_str(e))
            LOG.error(msg)
            raise HTTPBadRequest(explanation=msg,
                                 request=req,
                                 content_type="text/plain")
        except exception.NotFound as e:
            msg = (_("Failed to find host to update: %s") %
                   utils.exception_to_str(e))
            LOG.error(msg)
            raise HTTPNotFound(explanation=msg,
                               request=req,
                               content_type="text/plain")
        except exception.Forbidden as e:
            msg = (_("Forbidden to update host: %s") %
                   utils.exception_to_str(e))
            LOG.error(msg)
            raise HTTPForbidden(explanation=msg,
                                request=req,
                                content_type="text/plain")
        except (exception.Conflict, exception.Duplicate) as e:
            LOG.error(utils.exception_to_str(e))
            raise HTTPConflict(body=_('Host operation conflicts'),
                               request=req,
                               content_type='text/plain')
        else:
            self.notifier.info('host.update', host_meta)

        return {'host_meta': host_meta}

class HostDeserializer(wsgi.JSONRequestDeserializer):
    """Handles deserialization of specific controller method requests."""

    def _deserialize(self, request):
        result = {}
        result["host_meta"] = utils.get_host_meta(request)
        return result

    def add_host(self, request):
        return self._deserialize(request)

    def update_host(self, request):
        return self._deserialize(request)

    def discover_host(self, request):
        return self._deserialize(request)
    
    def add_discover_host(self, request):
        return self._deserialize(request)
    
    def update_discover_host(self, request):
        return self._deserialize(request)

class HostSerializer(wsgi.JSONResponseSerializer):
    """Handles serialization of specific controller method responses."""

    def __init__(self):
        self.notifier = notifier.Notifier()

    def add_host(self, response, result):
        host_meta = result['host_meta']
        response.status = 201
        response.headers['Content-Type'] = 'application/json'
        response.body = self.to_json(dict(host=host_meta))
        return response

    def delete_host(self, response, result):
        host_meta = result['host_meta']
        response.status = 201
        response.headers['Content-Type'] = 'application/json'
        response.body = self.to_json(dict(host=host_meta))
        return response

    def get_host(self, response, result):
        host_meta = result['host_meta']
        response.status = 201
        response.headers['Content-Type'] = 'application/json'
        response.body = self.to_json(dict(host=host_meta))
        return response
        
    def discover_host(self, response, result):
        host_meta = result
        response.status = 201
        response.headers['Content-Type'] = 'application/json'
        response.body = self.to_json(dict(host_meta))
        return response
    
    def add_discover_host(self, response, result):
        host_meta = result['host_meta']
        response.status = 201
        response.headers['Content-Type'] = 'application/json'
        response.body = self.to_json(dict(host=host_meta))
        return response
        
    def update_discover_host(self, response, result):
        host_meta = result['host_meta']
        response.status = 201
        response.headers['Content-Type'] = 'application/json'
        response.body = self.to_json(dict(host=host_meta))
    
    def get_discover_host_detail(self, response, result):
        host_meta = result['host_meta']
        response.status = 201
        response.headers['Content-Type'] = 'application/json'
        response.body = self.to_json(dict(host=host_meta))
        return response
        
def create_resource():
    """Hosts resource factory method"""
    deserializer = HostDeserializer()
    serializer = HostSerializer()
    return wsgi.Resource(Controller(), deserializer, serializer)

