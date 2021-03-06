#!/bin/bash

# 提供yum设置相关函数

if [ ! "$_DAISY_YUM_FILE" ];then

function make_yum_server
{
    path=`pwd`
    createrepo --update $path &>/dev/null
    [ "$?" -ne 0 ] && { echo "createrepo in $path failed"; exit 1; }
    patch_path="/home/daisy_patch/"
    if [ -d $patch_path ];then
        #避免旧的repodata被破坏，导致createrepo命令失败
        [ -d $patch_path/repodata ] && rm -rf $patch_path/repodata
        createrepo --update $patch_path &>/dev/null
        [ "$?" -ne 0 ] && { echo "createrepo in $patch_path failed"; exit 1; }
    fi
}

# 产生REPO客户端的文件
function make_yum_client
{
    path=`pwd`
    repo_name=`uuidgen`
    patch_repo_name=daisy_depend
    daisy_repofile=/etc/yum.repos.d/daisy_install_temp.repo
    daisy_depend_repofile=/etc/yum.repos.d/daisy_depend_temp.repo
    os_iso_repofile=/etc/yum.repos.d/OS_ISO.repo
    tfg_depend_repofile=/etc/yum.repos.d/TFG_DEPEND_PACKAGE.repo
    echo -e "[$repo_name]\nname=$repo_name\nbaseurl=file:$path\nenabled=1\ngpgcheck=0" > $daisy_repofile
    if [ -d $patch_path ];then
    echo -e "[$patch_repo_name]\nname=$patch_repo_name\nbaseurl=file:$patch_path\nenabled=1\ngpgcheck=0" > $daisy_depend_repofile
        daisy_yum="yum -y --disablerepo=* --enablerepo=$repo_name --enablerepo=$patch_repo_name"
    else
        daisy_yum="yum -y --disablerepo=* --enablerepo=$repo_name"
    fi
    if [ -f "$os_iso_repofile" ];then
        daisy_yum="${daisy_yum}"" --enablerepo=OS_ISO"
    fi
    if [ -f "$tfg_depend_repofile" ];then
        daisy_yum="${daisy_yum}"" --enablerepo=TFG_DEPEND_PACKAGE"
    fi
    yum clean all &>/dev/null
    $daisy_yum install httpd
    systemctl enable httpd
    systemctl start httpd || { echo "httpd start failed.";  exit 1; }
    httpd_path="/var/www/html/daisy_install"
    chmod 777 $path
    rm -f $httpd_path
    ln -s $path $httpd_path
}
function yum_set
{
    rm -rf /var/lib/rpm/__db.*
    echo "creating yum repo, please wait for several seconds..."
    make_yum_server
    make_yum_client
}

fi
export _DAISY_YUM_FILE="daisy_yum.sh"
