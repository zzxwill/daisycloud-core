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
/install endpoint for tecs API
"""
import os
import copy
import subprocess
import time

import traceback
import webob.exc
from oslo_config import cfg
from oslo_log import log as logging
from webob.exc import HTTPBadRequest
from webob.exc import HTTPForbidden

from threading import Thread

from daisy import i18n
from daisy import notifier

from daisy.common import exception
import daisy.registry.client.v1.api as registry
import daisy.api.backends.common as daisy_cmn
import daisy.api.backends.tecs.common as tecs_cmn

try:
    import simplejson as json
except ImportError:
    import json

LOG = logging.getLogger(__name__)
_ = i18n._
_LE = i18n._LE
_LI = i18n._LI
_LW = i18n._LW
tecs_state = tecs_cmn.TECS_STATE
def _get_service_disk_for_disk_array(req, role_id):
    disk_info = []
    service_disks = tecs_cmn.get_service_disk_list(req, {'filters': {'role_id': role_id}})
    for service_disk in service_disks:
        share_disk = {}
        if service_disk['disk_location'] == 'share':
            share_disk['service'] = service_disk['service']
            share_disk['lun'] = service_disk['lun']
            share_disk['data_ips'] = service_disk['data_ips'].split(',')
            share_disk['lvm_config'] = {}
            share_disk['lvm_config']['size'] = service_disk['size']
            share_disk['lvm_config']['vg_name'] = 'vg_%s' % service_disk['service']
            share_disk['lvm_config']['lv_name'] = 'lv_%s' % service_disk['service']
            share_disk['lvm_config']['fs_type'] = 'ext4'
            disk_info.append(share_disk)
    return disk_info

def _get_cinder_volume_for_disk_array(req, role_id):
    cinder_volume_info = []
    cinder_volumes = tecs_cmn.get_cinder_volume_list(req, {'filters': {'role_id': role_id}})
    for cinder_volume in cinder_volumes:
        cv_info = {}
        cv_info['management_ips'] = cinder_volume['management_ips'].split(',')
        cv_info['data_ips'] = cinder_volume['data_ips'].split(',')
        cv_info['user_name'] = cinder_volume['user_name']
        cv_info['user_pwd'] = cinder_volume['user_pwd']
        index = cinder_volume['backend_index']
        cv_info['backend'] = {index:{}}
        cv_info['backend'][index]['volume_driver'] = cinder_volume['volume_driver']
        cv_info['backend'][index]['volume_type'] = cinder_volume['volume_type']
        cv_info['backend'][index]['pools'] = cinder_volume['pools'].split(',')
        cinder_volume_info.append(cv_info)
    return cinder_volume_info

def get_disk_array_info(req, cluster_id):
    share_disk_info = []
    volume_disk_info = {}
    cinder_volume_disk_list = []
    roles = daisy_cmn.get_cluster_roles_detail(req,cluster_id)
    for role in roles:
        if role['deployment_backend'] != daisy_cmn.tecs_backend_name:
            continue
        if role['name'] == 'CONTROLLER_HA':
            share_disks = _get_service_disk_for_disk_array(req, role['id']) 
            share_disk_info += share_disks 
            cinder_volumes = _get_cinder_volume_for_disk_array(req, role['id']) 
            cinder_volume_disk_list += cinder_volumes
    if cinder_volume_disk_list:
        volume_disk_info['disk_array'] = cinder_volume_disk_list
    return (share_disk_info, volume_disk_info)

def get_host_min_mac(host_interfaces):
    macs = [interface['mac'] for interface in host_interfaces
            if interface['type'] == 'ether' and interface['mac']]
    macs.sort()
    return macs[0]

def get_ha_and_compute_ips(req, cluster_id):
    controller_ha_nodes = {}
    computer_ips = []

    roles = daisy_cmn.get_cluster_roles_detail(req,cluster_id)
    cluster_networks = daisy_cmn.get_cluster_networks_detail(req, cluster_id)
    for role in roles:
        if role['deployment_backend'] != daisy_cmn.tecs_backend_name:
            continue
        role_hosts = daisy_cmn.get_hosts_of_role(req, role['id'])
        for role_host in role_hosts:
            #host has installed tecs are exclusive
            if (role_host['status'] == tecs_state['ACTIVE'] or
                role_host['status'] == tecs_state['UPDATING'] or
                role_host['status'] == tecs_state['UPDATE_FAILED']):
                continue
            host_detail = daisy_cmn.get_host_detail(req,
                                                    role_host['host_id'])
            host_ip = tecs_cmn.get_host_network_ip(req,
                                                   host_detail,
                                                   cluster_networks,
                                                   'MANAGEMENT')
            if role['name'] == "CONTROLLER_HA":
                pxe_mac = [interface['mac'] for interface in host_detail['interfaces']
                                if interface['is_deployment'] == True]
                if pxe_mac and pxe_mac[0]:
                    controller_ha_nodes[host_ip] = pxe_mac[0]
                else:
                    min_mac = get_host_min_mac(host_detail['interfaces'])
                    controller_ha_nodes[host_ip] = min_mac
            if role['name'] == "COMPUTER":
                computer_ips.append(host_ip)
    return (controller_ha_nodes, computer_ips)
    
def config_ha_share_disk(share_disk_info, controller_ha_nodes):
    
    error_msg = ""
    cmd = 'rm -rf /var/lib/daisy/tecs/storage_auto_config/base/*.json'
    daisy_cmn.subprocess_call(cmd)
    with open("/var/lib/daisy/tecs/storage_auto_config/base/control.json", "w") as fp:
        json.dump(share_disk_info, fp, indent=2)
        
        
    for host_ip in controller_ha_nodes.keys():
        password = "ossdbg1"
        cmd = '/var/lib/daisy/tecs/trustme.sh %s %s' % (host_ip, password)
        daisy_cmn.subprocess_call(cmd)
        cmd = 'clush -S -w %s "mkdir -p /home/tecs_install"' % (host_ip,)
        daisy_cmn.subprocess_call(cmd)
        try:
            scp_bin_result = subprocess.check_output(
                'scp -o StrictHostKeyChecking=no -r /var/lib/daisy/tecs/storage_auto_config %s:/home/tecs_install' % (host_ip,),
                shell=True, stderr=subprocess.STDOUT)
        except subprocess.CalledProcessError as e:
            error_msg = "scp /var/lib/daisy/tecs/storage_auto_config to %s failed!" % host_ip
            return error_msg
        try:
            LOG.info(_("Config share disk for host %s" % host_ip))
            cmd = "cd /home/tecs_install/storage_auto_config/; python storage_auto_config.py share_disk %s" % controller_ha_nodes[host_ip]
            exc_result = subprocess.check_output('clush -S -w %s "%s"' % (host_ip,cmd),
                                                  shell=True, stderr=subprocess.STDOUT)
        except subprocess.CalledProcessError as e:
            LOG.info(_("Storage script error message: %s" % e.output))
            error_msg = "config Disk Array share disks on %s failed!" % host_ip
            return error_msg
    return error_msg

def config_ha_cinder_volume(volume_disk_info, controller_ha_ips):
    error_msg = ""
    cmd = 'rm -rf /var/lib/daisy/tecs/storage_auto_config/base/*.json'
    daisy_cmn.subprocess_call(cmd)
    with open("/var/lib/daisy/tecs/storage_auto_config/base/cinder.json", "w") as fp:
        json.dump(volume_disk_info, fp, indent=2)
    for host_ip in controller_ha_ips:
        password = "ossdbg1"
        cmd = '/var/lib/daisy/tecs/trustme.sh %s %s' % (host_ip, password)
        daisy_cmn.subprocess_call(cmd)
        cmd = 'clush -S -w %s "mkdir -p /home/tecs_install"' % (host_ip,)
        daisy_cmn.subprocess_call(cmd)
        try:
            scp_bin_result = subprocess.check_output(
                'scp -o StrictHostKeyChecking=no -r /var/lib/daisy/tecs/storage_auto_config %s:/home/tecs_install' % (host_ip,),
                shell=True, stderr=subprocess.STDOUT)
        except subprocess.CalledProcessError as e:
            error_msg = "scp /var/lib/daisy/tecs/storage_auto_config to %s failed!" % host_ip
            return error_msg
        try:
            LOG.info(_("Config cinder volume for host %s" % host_ip))
            cmd = 'cd /home/tecs_install/storage_auto_config/; python storage_auto_config.py cinder_conf %s' % host_ip
            exc_result = subprocess.check_output('clush -S -w %s "%s"' % (host_ip,cmd),
                                                  shell=True, stderr=subprocess.STDOUT)
        except subprocess.CalledProcessError as e:
            LOG.info(_("Storage script error message: %s" % e.output))
            error_msg = "config Disk Array cinder volumes on %s failed!" % host_ip
            return error_msg
    return error_msg
        
def config_compute_multipath(all_nodes_ip):
    error_msg = ""
    for host_ip in all_nodes_ip:
        password = "ossdbg1"
        cmd = '/var/lib/daisy/tecs/trustme.sh %s %s' % (host_ip, password)
        daisy_cmn.subprocess_call(cmd)
        cmd = 'clush -S -w %s "mkdir -p /home/tecs_install"' % (host_ip,)
        daisy_cmn.subprocess_call(cmd)
        try:
            scp_bin_result = subprocess.check_output(
                'scp -o StrictHostKeyChecking=no -r /var/lib/daisy/tecs/storage_auto_config %s:/home/tecs_install' % (host_ip,),
                shell=True, stderr=subprocess.STDOUT)
        except subprocess.CalledProcessError as e:
            error_msg = "scp /var/lib/daisy/tecs/storage_auto_config to %s failed!" % host_ip
            return error_msg
        try:
            LOG.info(_("Config multipath for host %s" % host_ip))
            cmd = 'cd /home/tecs_install/storage_auto_config/; python storage_auto_config.py check_multipath'
            exc_result = subprocess.check_output('clush -S -w %s "%s"' % (host_ip,cmd),
                                                  shell=True, stderr=subprocess.STDOUT)
        except subprocess.CalledProcessError as e:
            LOG.info(_("Storage script error message: %s" % e.output))
            error_msg = "config Disk Array multipath on %s failed!" % host_ip
            return error_msg
    return error_msg