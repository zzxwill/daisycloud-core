#!/bin/bash
# 提供和yum安装相关的公共函数和变量
if [ ! "$_INSTALL_INTERFACE_FILE" ];then
_INSTALL_INTERFACE_DIR=`pwd`
cd $_INSTALL_INTERFACE_DIR/../common/
.  daisy_global_var.sh
.  daisy_common_func.sh
cd $_INSTALL_INTERFACE_DIR
.  install_global_var.sh
.  install_func.sh

daisy_file="/etc/daisy/daisy-registry.conf"
db_name="daisy"
ironic_name="ironic"
keystone_db_name="keystone"
keystone_admin_token="e93e9abf42f84be48e0996e5bd44f096"
daisy_install="/var/log/daisy/daisy_install"
installdatefile=`date -d "today" +"%Y%m%d-%H%M%S"`
install_logfile=$daisy_install/daisyinstall_$installdatefile.log
#输出的内容既显示在屏幕上又输出到指定文件中
function write_install_log
{
    local promt="$1"
    echo -e "$promt"
    echo -e "`date -d today +"%Y-%m-%d %H:%M:%S"`  $promt" >> $install_logfile
}
#安装
function all_install
{
    echo "*******************************************************************************"
    echo "daisy will installed  ..." 
    echo "*******************************************************************************"
    
    if [ ! -d "$daisy_install" ];then
        mkdir -p $daisy_install
    fi
 
    if [ ! -f "$install_logfile" ];then
        touch $install_logfile
    fi 

    sudo rm -rf /root/.my.cnf
    [ "$?" -ne 0 ] && { write_install_log "Error:can not rm of /root/.my.cnf file"; exit 1; } 
    write_install_log "install fping rpm"
    install_rpm_by_yum "fping"
    
    write_install_log "install mariadb-galera-server rpm"
    install_rpm_by_yum "mariadb-galera-server"
  
    write_install_log "install mariadb rpm"
    install_rpm_by_yum "mariadb"
    
    write_install_log "install rabbitmq-server rpm"
    install_rpm_by_yum "rabbitmq-server"
    
    write_install_log "install keystone rpm"
    install_rpm_by_yum "openstack-keystone"
    
    write_install_log "install ironic rpm"
    install_rpm_by_yum "openstack-ironic-api openstack-ironic-common openstack-ironic-conductor python-ironicclient" 
    
    write_install_log "install ironic-discoverd rpm"
    install_rpm_by_yum "openstack-ironic-discoverd python-ironic-discoverd"
    
    write_install_log "install daisy rpm"
    install_rpm_by_yum "daisy"
    
    write_install_log "install daisy dashboard rpm"
    install_rpm_by_yum "python-django-horizon-doc"
    install_rpm_by_yum "daisy-dashboard"
    
    write_install_log "install pxe server rpm"
    install_rpm_by_yum pxe_server_install

    # 获取管理网ip地址，然后把数据库的daisy用户更新到配置文件中
    get_public_ip
    if [ -z $public_ip ];then
        write_install_log "Error:default gateway is not set!!!"
        exit 1
    else
        update_section_config "$daisy_file" database connection "mysql://daisy:daisy@$public_ip/$db_name?charset=utf8"
        config_keystone_local_setting
    fi
    
    systemctl restart openstack-keystone.service
    [ "$?" -ne 0 ] && { write_install_log "Error:systemctl restart openstack-keystone.service failed"; exit 1; }
    systemctl restart httpd.service
    [ "$?" -ne 0 ] && { write_install_log "Error:systemctl restart httpd.service failed"; exit 1; }
    systemctl start daisy-api.service
    [ "$?" -ne 0 ] && { write_install_log "Error:systemctl start daisy-api.service failed"; exit 1; }
    systemctl start daisy-registry.service
    [ "$?" -ne 0 ] && { write_install_log "Error:systemctl start daisy-registry.service failed"; exit 1; }
    systemctl start mariadb.service
    [ "$?" -ne 0 ] && { write_install_log "Error:systemctl start mariadb.service failed"; exit 1; }
    
    
    systemctl enable openstack-keystone.service  >> $install_logfile 2>&1
    systemctl enable httpd.service  >> $install_logfile 2>&1
    systemctl enable daisy-api.service >> $install_logfile 2>&1
    systemctl enable daisy-registry.service >> $install_logfile 2>&1
    systemctl enable mariadb.service >> $install_logfile 2>&1
    

    mysql_cmd="mysql"
    local mariadb_result=`systemctl is-active mariadb.service`
    if [ $? -eq 0 ];then
        # 创建keystone数据库
        local create_keystone_sql="create database IF NOT EXISTS $keystone_db_name default charset=utf8"
        write_install_log "create $keystone_db_name database in mariadb"
        echo ${create_keystone_sql} | ${mysql_cmd}
        if [ $? -ne 0 ];then
            write_install_log "Error:create $keystone_db_name database failed..."
            exit 1
        fi
        
        # 创建daisy数据库
        local create_db_sql="create database IF NOT EXISTS $db_name default charset=utf8"
        write_install_log "create $db_name database in mariadb"
        echo ${create_db_sql} | ${mysql_cmd}
        if [ $? -ne 0 ];then
            write_install_log "Error:create $db_name database failed..."
            exit 1
        fi
        
        # 创建ironic数据库
        local create_ironic_sql="create database IF NOT EXISTS $ironic_name default charset=utf8"
        write_install_log "create $ironic_name database in mariadb"
        echo ${create_ironic_sql} | ${mysql_cmd}
        if [ $? -ne 0 ];then
            write_install_log "Error:create $ironic_name database failed..."
            exit 1
        fi
        
        # 创建keystone用户
        write_install_log "create keystone user in mariadb"
        echo "grant all privileges on *.* to 'keystone'@'localhost' identified by 'keystone'" | ${mysql_cmd}
        if [ $? -ne 0 ];then
            write_install_log "Error:create keystone user failed..."
            exit 1
        fi

        # 创建daisy用户
        write_install_log "create daisy user in mariadb"
        echo "grant all privileges on *.* to 'daisy'@'localhost' identified by 'daisy'" | ${mysql_cmd}
        if [ $? -ne 0 ];then
            write_install_log "Error:create daisy user failed..."
            exit 1
        fi
        
        # 创建ironic用户
        write_install_log "create ironic user in mariadb"
        echo "grant all privileges on ironic.* to 'ironic'@'localhost' identified by 'ironic'" | ${mysql_cmd}
        if [ $? -ne 0 ];then
            write_install_log "Error:create ironic user failed..."
            exit 1
        fi

        # 给keystone数据库赋予主机访问权限
        write_install_log "Give the host access to the keystone database"
        echo "grant all privileges on keystone.* to 'keystone'@'%' identified by 'keystone'"| ${mysql_cmd}
        if [ $? -ne 0 ];then
            write_install_log "Error:Give the host access to the keystone database failed..."
            exit 1
        fi
        
        # 给daisy数据库赋予主机访问权限
        write_install_log "Give the host access to the daisy database"
        echo "grant all privileges on daisy.* to 'daisy'@'%' identified by 'daisy'"| ${mysql_cmd}
        if [ $? -ne 0 ];then
            write_install_log "Error:Give the host access to the daisy database failed..."
            exit 1
        fi
        
        # 给ironic数据库赋予主机访问权限
        write_install_log "Give the host access to the ironic database"
        echo "grant all privileges on ironic.* to 'ironic'@'%' identified by 'ironic'"| ${mysql_cmd}
        if [ $? -ne 0 ];then
            write_install_log "Error:Give the host access to the ironic database failed..."
            exit 1
        fi
        
        echo "flush privileges"| ${mysql_cmd}

    else 
        write_install_log "Error:mariadb service is not active"
        exit 1
    fi
    
    
    #创建keystone数据库的表
    which keystone-manage >> $install_logfile 2>&1
    if [ "$?" == 0 ];then
        write_install_log "start keystone-manage db_sync..." 
        keystone-manage db_sync
        [ "$?" -ne 0 ] && { write_install_log "Error:keystone-manage db_sync command failed"; exit 1; } 
    fi
    #创建horizon admin账户
    export OS_SERVICE_TOKEN=$keystone_admin_token
    export OS_SERVICE_ENDPOINT=http://$public_ip:35357/v2.0
    keystone user-create --name=admin --pass=keystone  >> $install_logfile 2>&1
    [ "$?" -ne 0 ] && { write_install_log "Error:keystone user-create command failed"; exit 1; }
    keystone role-create --name=admin  >> $install_logfile 2>&1
    [ "$?" -ne 0 ] && { write_install_log "Error:keystone role-create command failed"; exit 1; }
    keystone tenant-create --name=admin --description="Admin Tenant"  >> $install_logfile 2>&1
    [ "$?" -ne 0 ] && { write_install_log "Error:keystone tenant-create command failed"; exit 1; }
    keystone user-role-add --user=admin --tenant=admin --role=admin  >> $install_logfile 2>&1
    [ "$?" -ne 0 ] && { write_install_log "Error:keystone user-role-add command failed"; exit 1; }
    #keystone user-role-add --user=admin --role=_member_ --tenant=admin  >> $install_logfile 2>&1
    keystone service-create --name keystone --type identity --description "OpenStack Identity Service" >> $install_logfile 2>&1
    [ "$?" -ne 0 ] && { write_install_log "Error:keystone service-create command failed"; exit 1; }
    service_id=`keystone service-list 2>/dev/null|grep "keystone" |awk -F '| ' '{print $2}'`
    if [ -z $service_id ];then
        write_install_log "Error:there is no service in keystone database"
        exit 1
    fi
    keystone endpoint-create --service-id=$service_id --region=RegionOne --publicurl=http://$public_ip:5000/v2.0 --internalurl=http://$public_ip:5000/v2.0 --adminurl=http://$public_ip:35357/v2.0 >> $install_logfile 2>&1
    [ "$?" -ne 0 ] && { write_install_log "Error:keystone endpoint-create command failed"; exit 1; }
    #创建daisy数据库的表
    which daisy-manage >> $install_logfile 2>&1
    if [ "$?" == 0 ];then
        write_install_log "start daisy-manage db_sync..." 
        daisy-manage db_sync
        [ "$?" -ne 0 ] && { write_install_log "Error:daisy-manage db_sync command failed"; exit 1; } 
    fi
    
    #增加rabbitmq相关的配置文件
    config_rabbitmq_env
    config_rabbitmq_config    
    
    #配置ironic相关的配置项
    config_ironic "/etc/ironic/ironic.conf"    
    config_ironic_discoverd "/etc/ironic-discoverd/discoverd.conf" "$public_ip"
    
    #修改clustershell的配置文件
    clustershell_conf="/etc/clustershell/clush.conf"
    sed  -i "s/connect_timeout:[[:space:]]*.*/connect_timeout: 360/g" $clustershell_conf
    sed  -i "s/command_timeout:[[:space:]]*.*/command_timeout: 3600/g" $clustershell_conf
    
    #创建ironic数据库的表
    which ironic-dbsync >> $install_logfile 2>&1
    if [ "$?" == 0 ];then
        write_install_log "start ironic-dbsync ..." 
        ironic-dbsync --config-file /etc/ironic/ironic.conf create_schema
        [ "$?" -ne 0 ] && { write_install_log "Error:ironic-dbsync --config-file /etc/ironic/ironic.conf create_schema failed"; exit 1; } 
    fi
    
    systemctl restart rabbitmq-server.service
    [ "$?" -ne 0 ] && { write_install_log "Error:systemctl restart rabbitmq-server.service failed"; exit 1; }
    
    systemctl restart openstack-keystone.service
    [ "$?" -ne 0 ] && { write_install_log "Error:systemctl restart rabbitmq-server.service failed"; exit 1; }
    
    systemctl restart openstack-ironic-api.service 
    [ "$?" -ne 0 ] && { write_install_log "Error:systemctl restart openstack-ironic-api.service failed"; exit 1; }
    systemctl restart openstack-ironic-conductor.service
    [ "$?" -ne 0 ] && { write_install_log "Error:systemctl restart openstack-ironic-conductor.service failed"; exit 1; }
    systemctl restart openstack-ironic-discoverd.service
    [ "$?" -ne 0 ] && { write_install_log "Error:systemctl restart openstack-ironic-discoverd.service failed"; exit 1; }
    
    systemctl start daisy-orchestration.service
    [ "$?" -ne 0 ] && { write_install_log "Error:systemctl start daisy-orchestration.service failed"; exit 1; }
    
    systemctl enable daisy-orchestration.service >> $install_logfile 2>&1
    systemctl enable openstack-ironic-api.service >> $install_logfile 2>&1
    systemctl enable openstack-ironic-conductor.service >> $install_logfile 2>&1
    systemctl enable openstack-ironic-discoverd.service >> $install_logfile 2>&1
    
    #daisy初始化，创建组件、服务、角色以及网络平面
    daisy_init_func
    
    modify_sudoers /etc/sudoers requiretty
    
    daisyrc_admin "$public_ip"
    
    build_pxe_server "$public_ip"
    
    config_get_node_info
    
    write_install_log "Daisy Install Successfull..."
}
_INSTALL_INTERFACE_FILE="install_interface.sh"

fi

