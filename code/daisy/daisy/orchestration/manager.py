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
/orchestration for tecs API
"""
import copy
import subprocess
import time

import traceback
import webob.exc
from oslo_config import cfg
from oslo_log import log as logging
from webob.exc import HTTPBadRequest
from webob.exc import HTTPForbidden
from webob.exc import HTTPServerError
from webob.exc import HTTPNotFound
import threading
from threading import Thread

from daisy import i18n
from daisy import notifier

from daisy.common import exception
from daisyclient.v1.client import Client
from eventlet import greenthread
import eventlet.timeout
LOG = logging.getLogger(__name__)
CONF = cfg.CONF


class OrchestrationManager():

    def __init__(self, *args, **kwargs):
        """Load orchestration options and initialization."""
        pass
    
    @staticmethod  
    def find_auto_scale_cluster():
        try:
            daisy_version = 1.0
            daisy_endpoint="http://127.0.0.1:19292"
            daisy_client = Client(version=daisy_version, endpoint=daisy_endpoint)
            orchestrationManager = OrchestrationManager()
            cluster_meta={'auto_scale':'1'}
            params = {'filters':cluster_meta}
            clusters_gen = daisy_client.clusters.list(**params)
            clusters = [cluster.to_dict() for cluster in clusters_gen if cluster.auto_scale == 1 ]
            if clusters:
                cluster_id = clusters[0]['id']
                params = {'filters':''}
                hosts_gen = daisy_client.hosts.list(**params)
                init_hosts = [host.to_dict() for host in hosts_gen if host.os_status =="init" or host.os_status == "install-failed"]
                if not init_hosts:
                    LOG.info("no init or install-failed host")
                    return {"status":"no init host"}
                
                params = {'filters':{'cluster_id':cluster_id}}
                roles_gen = daisy_client.roles.list(**params)
                roles_in_cluster = [role.to_dict() for role in roles_gen]
                roles = [role for role in roles_in_cluster if role['name'] =="CONTROLLER_HA" and role['status'] == "active"]
                if not roles:
                    LOG.info("no active CONTROLLER_HA role")
                    return {"status":"no active CONTROLLER_HA role"}
                for host in init_hosts:
                    if host['status'] == "init":
                        host_info = daisy_client.hosts.get(host['id'])
                        if hasattr(host_info, "interfaces"):
                            scale_host_info = orchestrationManager.set_scale_host_interface(cluster_id, host_info,daisy_client)
                            if scale_host_info:
                                host_meta ={'name':scale_host_info.name,'os_version':scale_host_info.os_version_file, 'root_lv_size':scale_host_info.root_lv_size,'swap_lv_size':scale_host_info.swap_lv_size, 'role':['COMPUTER'], 'cluster':cluster_id, 'interfaces':scale_host_info.interfaces }
                                daisy_client.hosts.update(host['id'],**host_meta)
                            else:
                                LOG.error("can not set scale host")
                                return {"status":"no scale host"}
                            
                        else:
                            LOG.info("not interfaces in host %s" % host['id'])
                            raise HTTPNotFound("not interfaces in host %s" % host['id'])
                orchestrationManager._os_tecs_install(cluster_id, daisy_client)
        except exception.Invalid as e:
            LOG.exception(e.message)
                    
        
    def _os_tecs_install(self, cluster_id, daisy_client):
        try:
            install_meta = {'cluster_id':cluster_id}
            daisy_client.install.install(**install_meta)
            LOG.info("install cluster %s" %cluster_id)
        except exception.Invalid as e:
            LOG.error("install error:%s" % e.message)

    def get_active_compute(self, cluster_id, daisy_client):
        host_meta={'cluster_id':cluster_id}
        host_meta['filters'] = host_meta
        host_list_generator = daisy_client.hosts.list(**host_meta)
        active_compute_list = []
        host_list = [host for host in host_list_generator if hasattr(host,"role_status") and host.role_status == "active"]
        for host in host_list:
            host_info = daisy_client.hosts.get(host.id)
            if hasattr(host_info,"role") and "COMPUTER" in host_info.role and hasattr(host_info,"interfaces"):
                active_compute_list.append(host_info)
        return active_compute_list
        
    def set_scale_host_interface(self, cluster_id, host_info, daisy_client):
        compute_list = []
        active_compute_host = None
        compute_list = self.get_active_compute(cluster_id, daisy_client)
        if compute_list and hasattr(host_info,"interfaces"):
            active_compute_host = self.check_isomorphic_host(compute_list, host_info.interfaces)
            if not active_compute_host:
                LOG.info("%s not isomorphic host" % host_info.name)
                return None
            host_info.os_version_file = active_compute_host.os_version_file
            host_info.root_lv_size = active_compute_host.root_lv_size
            host_info.swap_lv_size = active_compute_host.swap_lv_size
            host_info.name="computer-" + host_info.name[-12:]
        else:
            LOG.error("no active compute node in cluster")
            return None
        
        if active_compute_host:
            for interface in host_info.interfaces:
                for compute_interface in active_compute_host.interfaces:
                    if interface['pci'] == compute_interface['pci'] and compute_interface.has_key("assigned_networks"):
                        for assigned_network in compute_interface['assigned_networks']:
                            assigned_network['ip'] = ''
                        interface['assigned_networks'] = compute_interface['assigned_networks']
                        interface['name'] = compute_interface['name']
                        interface['netmask'] = compute_interface['netmask']
                        interface['gateway'] = compute_interface['gateway']
                        interface['mode'] = compute_interface['mode']
            for compute_interface in active_compute_host.interfaces:
                for assigned_network in compute_interface['assigned_networks']:
                    assigned_network['ip'] = ''
                compute_interface['host_id'] = host_info.id
                if compute_interface['type'] == "bond":
                    interfaces = [interface for interface in host_info.interfaces if interface['name'] == compute_interface['name']]
                    if not interfaces:
                        host_info.interfaces.append(compute_interface)
        return host_info
    
    def check_isomorphic_host(self, compute_list, new_interfaces):
        for compute_host in compute_list:
            new_interface_count = len([interface for interface in new_interfaces if interface['type'] =="ether"])
            compute_interface_count = len([interface for interface in compute_host.interfaces if interface['type'] =="ether"])
            if new_interface_count != compute_interface_count:
                continue
            is_isomorphic = False
            for interface in new_interfaces:
                if interface['type'] != "ether":
                    continue
                for compute_interface in compute_host.interfaces:
                    if interface['pci'] == compute_interface['pci'] and interface['max_speed'] == compute_interface['max_speed']:
                        is_isomorphic = True
                    elif interface['pci'] == compute_interface['pci'] and interface['max_speed'] != compute_interface['max_speed']:
                        is_isomorphic = False
                        break
                if not is_isomorphic:
                    break
            if is_isomorphic:
                return compute_host
        return False
                    
