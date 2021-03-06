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

from sqlalchemy import MetaData, Table, Column, String


meta = MetaData()
db_vip = Column('db_vip', String(255))
glance_vip = Column('glance_vip', String(255))
mongodb_vip = Column('mongodb_vip', String(255))


def upgrade(migrate_engine):
    meta.bind = migrate_engine


    roles = Table('roles', meta, autoload=True)
    roles.create_column(db_vip)
    roles.create_column(glance_vip)
    roles.create_column(mongodb_vip)



    

