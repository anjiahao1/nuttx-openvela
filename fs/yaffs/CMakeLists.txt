# ##############################################################################
# fs/yaffs/CMakeLists.txt
#
# Licensed to the Apache Software Foundation (ASF) under one or more contributor
# license agreements.  See the NOTICE file distributed with this work for
# additional information regarding copyright ownership.  The ASF licenses this
# file to you under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License.  You may obtain a copy of
# the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.  See the
# License for the specific language governing permissions and limitations under
# the License.
#
# ##############################################################################

if(CONFIG_FS_YAFFS)

  file(GLOB YAFFS_CSRCS ${CMAKE_CURRENT_LIST_DIR}/yaffs/*.c)
  file(GLOB DIRECT_CSRCS ${CMAKE_CURRENT_LIST_DIR}/yaffs/direct/*.c)

  list(
    REMOVE_ITEM
    YAFFS_CSRCS
    ${CMAKE_CURRENT_LIST_DIR}/yaffs/yaffs_attribs.c
    ${CMAKE_CURRENT_LIST_DIR}/yaffs/yaffs_mtdif_multi.c
    ${CMAKE_CURRENT_LIST_DIR}/yaffs/yaffs_mtdif_single.c
    ${CMAKE_CURRENT_LIST_DIR}/yaffs/yaffs_vfs_multi.c
    ${CMAKE_CURRENT_LIST_DIR}/yaffs/yaffs_vfs_single.c)

  target_compile_definitions(
    fs PRIVATE -DCONFIG_YAFFS_DIRECT -DCONFIG_YAFFS_PROVIDE_DEFS
               -DCONFIG_YAFFS_DEFINES_TYPES -Dloff_t=off_t)

  target_sources(fs PRIVATE ${YAFFS_CSRCS} ${DIRECT_CSRCS})

  target_include_directories(fs PRIVATE ${CMAKE_CURRENT_LIST_DIR}/yaffs)

  if(CONFIG_TRACE_LITTLEFS_FS)
    set_source_files_properties(yaffs_vfs.c DIRECTORY ..
                                PROPERTIES COMPILE_FLAGS -finstrument-functions)
  endif()
endif()
