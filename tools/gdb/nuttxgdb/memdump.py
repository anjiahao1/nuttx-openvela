############################################################################
# tools/gdb/nuttxgdb/memdump.py
#
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.  The
# ASF licenses this file to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance with the
# License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.  See the
# License for the specific language governing permissions and limitations
# under the License.
#
############################################################################

import argparse
import bisect
import time

import gdb

from . import utils
from .lists import NxSQueue, sq_count
from .utils import get_long_type, get_symbol_value, get_tcb, lookup_type, read_ulong

MM_ALLOC_BIT = 0x1
MM_PREVFREE_BIT = 0x2
MM_MASK_BIT = MM_ALLOC_BIT | MM_PREVFREE_BIT
MEMPOOL_MAGIC_ALLOC = 0x55555555

PID_MM_ORPHAN = -6
PID_MM_BIGGEST = -5
PID_MM_FREE = -4
PID_MM_ALLOC = -3
PID_MM_LEAK = -2
PID_MM_MEMPOOL = -1

mm_allocnode_type = lookup_type("struct mm_allocnode_s")
sizeof_size_t = lookup_type("size_t").sizeof
sizeof_sq_entry_t = lookup_type("sq_entry_t").sizeof
mempool_backtrace_type = lookup_type("struct mempool_backtrace_s")
mempool_s_type = utils.lookup_type("struct mempool_s")
mempool_procfs_entry_type = utils.lookup_type("struct mempool_procfs_entry_s")

CONFIG_MM_BACKTRACE = get_symbol_value("CONFIG_MM_BACKTRACE")
CONFIG_MM_DFAULT_ALIGNMENT = get_symbol_value("CONFIG_MM_DFAULT_ALIGNMENT")


def align_up(size, align) -> int:
    """Align the size to the specified alignment"""
    return (size + (align - 1)) & ~(align - 1)


def mm_nodesize(size) -> int:
    """Return the real size of a memory node"""
    return size & ~MM_MASK_BIT


def mm_node_is_alloc(size) -> bool:
    """Return node is allocated according to recorded size"""
    return size & MM_ALLOC_BIT != 0


def mm_prevnode_is_free(size) -> bool:
    """Return prevnode is free according to recorded size"""
    return size & MM_PREVFREE_BIT != 0


def mm_foreach(heap):
    """Iterate over a heap, yielding each node"""
    nregions = get_symbol_value("CONFIG_MM_REGIONS")
    heapstart = heap["mm_heapstart"]
    heapend = heap["mm_heapend"]

    for region in range(0, nregions):
        start = heapstart[region]
        end = heapend[region]
        node = start
        while node <= end:
            yield node
            next = int(node) + mm_nodesize(node["size"])
            next = gdb.Value(next).cast(mm_allocnode_type.pointer())
            if node == next:
                gdb.write(f"Error: maybe have memory fault on {hex(node)}\n")
                break
            node = next


def mm_dumpnode(node, count, align, simple, detail, alive):
    if node["size"] & MM_ALLOC_BIT != 0:
        charnode = int(node)
        if not alive:
            # if pid is not alive put a red asterisk.
            gdb.write("\x1b[33;1m*\x1b[m")

        if not detail:
            gdb.write("%*d" % (6 if alive else 5, count))

        gdb.write(
            "%6d%12u%12u%#*x"
            % (
                node["pid"],
                mm_nodesize(node["size"]),
                node["seqno"],
                align,
                charnode + mm_allocnode_type.sizeof,
            )
        )

        if node.type.has_key("backtrace"):
            firstrow = True
            for backtrace in utils.ArrayIterator(node["backtrace"]):
                if int(backtrace) == 0:
                    break

                if simple:
                    gdb.write(" %0#*x" % (align, int(backtrace)))
                else:
                    if firstrow:
                        firstrow = False
                    else:
                        if not detail:
                            gdb.write(" " * 6)
                        gdb.write(" " * (6 + 12 + 12 + align))
                    gdb.write(
                        "  [%0#*x] %-20s %s:%d\n"
                        % (
                            align,
                            int(backtrace),
                            backtrace.format_string(
                                raw=False, symbols=True, address=False
                            ),
                            gdb.find_pc_line(int(backtrace)).symtab,
                            gdb.find_pc_line(int(backtrace)).line,
                        )
                    )

    else:
        charnode = int(node)
        gdb.write(
            "%12u%#*x"
            % (
                mm_nodesize(node["size"]),
                align,
                charnode + mm_allocnode_type.sizeof,
            )
        )

    gdb.write("\n")


def mempool_multiple_foreach(mpool):
    """Iterate over all pools in a mempool, yielding each pool"""
    i = 0
    while i < mpool["npools"]:
        pool = mpool["pools"] + i
        yield pool
        i += 1


def mempool_align() -> int:
    if CONFIG_MM_DFAULT_ALIGNMENT:
        align = CONFIG_MM_DFAULT_ALIGNMENT
    else:
        align = 2 * sizeof_size_t
    return align


def mempool_realblocksize(pool):
    """Return the real block size of a mempool"""

    if CONFIG_MM_BACKTRACE >= 0:
        return align_up(
            pool["blocksize"] + mempool_backtrace_type.sizeof,
            mempool_align(),
        )
    else:
        return pool["blocksize"]


def get_backtrace(node):

    backtrace_list = []
    max = node["backtrace"].type.range()[1]
    for x in range(0, max):
        if node["backtrace"][x] != 0:
            backtrace_list.append(int(node["backtrace"][x]))
        else:
            break

    return tuple(backtrace_list)


def record_backtrace(node, size, backtrace_dict):
    if node.type.has_key("backtrace"):
        backtrace = get_backtrace(node)
        if (backtrace, int(node["pid"])) not in backtrace_dict.keys():
            info = {}
            info["node"] = node
            info["count"] = 1
            info["size"] = size
            info["pid"] = node["pid"]
            backtrace_dict[(backtrace, int(node["pid"]))] = info
        else:
            backtrace_dict[(backtrace, int(node["pid"]))]["count"] += 1

    return backtrace_dict


def get_count(element):
    return element["count"]


def mempool_foreach(pool):
    """Iterate over all block in a mempool"""

    sq_entry_type = lookup_type("sq_entry_t")

    blocksize = mempool_realblocksize(pool)
    if pool["ibase"] != 0:
        nblk = pool["interruptsize"] / blocksize
        while nblk > 0:
            bufaddr = gdb.Value(pool["ibase"] + nblk * blocksize + pool["blocksize"])
            buf = bufaddr.cast(mempool_backtrace_type.pointer())
            yield buf
            nblk -= 1

    for entry in NxSQueue(pool["equeue"]):
        nblk = (pool["expandsize"] - sq_entry_type.sizeof) / blocksize
        base = int(entry) - nblk * blocksize
        while nblk > 0:
            nblk -= 1
            bufaddr = gdb.Value(base + nblk * blocksize + pool["blocksize"])
            buf = bufaddr.cast(mempool_backtrace_type.pointer())
            yield buf


def mempool_dumpbuf(buf, blksize, count, align, simple, detail, alive):
    charnode = gdb.Value(buf).cast(lookup_type("char").pointer())

    if not alive:
        # if pid is not alive put a red asterisk.
        gdb.write("\x1b[33;1m*\x1b[m")

    if not detail:
        gdb.write("%*d" % (6 if alive else 5, count))

    gdb.write(
        "%6d%12u%12u%#*x"
        % (
            buf["pid"],
            blksize,
            buf["seqno"],
            align,
            (int)(charnode - blksize),
        )
    )

    if buf.type.has_key("backtrace"):
        max = buf["backtrace"].type.range()[1]
        firstrow = True
        for x in range(0, max):
            backtrace = int(buf["backtrace"][x])
            if backtrace == 0:
                break

            if simple:
                gdb.write(" %0#*x" % (align, backtrace))
            else:
                if firstrow:
                    firstrow = False
                else:
                    if not detail:
                        gdb.write(" " * 6)
                    gdb.write(" " * (6 + 12 + 12 + align))
                gdb.write(
                    "  [%0#*x] %-20s %s:%d\n"
                    % (
                        align,
                        int(backtrace),
                        buf["backtrace"][x].format_string(
                            raw=False, symbols=True, address=False
                        ),
                        gdb.find_pc_line(int(backtrace)).symtab,
                        gdb.find_pc_line(int(backtrace)).line,
                    )
                )

    gdb.write("\n")


def check_node_alive(node) -> bool:
    if CONFIG_MM_BACKTRACE <= 0:
        return True
    else:
        tcb = get_tcb(node["pid"])
        return tcb is not None


class HeapNode:
    def __init__(self, gdb_node, nextfree=False):
        self.gdb_node = gdb_node

        record_size = gdb_node["size"]

        try:
            seqno = gdb_node["seqno"]
        except gdb.error:
            seqno = 0

        try:
            node_pid = gdb_node["pid"]
        except gdb.error:
            node_pid = 0

        self.size = mm_nodesize(record_size)
        self.alloc = mm_node_is_alloc(record_size)
        self.seqno = seqno
        self.pid = node_pid
        self.alive = check_node_alive(gdb_node)
        self.base = int(gdb_node)
        self.prevfree = mm_prevnode_is_free(record_size)
        self.nextfree = nextfree

    def __lt__(self, other):
        return self.size < other.size

    def inside_sequence(self, seqmin, seqmax):
        return self.seqno >= seqmin and self.seqno <= seqmax

    def contains_address(self, address):
        return address >= self.base and address < self.base + self.size

    def is_orphan(self):
        return self.prevfree or self.nextfree

    def dump(self, detail, simple, align, backtrace_dict):
        if detail:
            mm_dumpnode(
                self.gdb_node,
                1,
                align,
                simple,
                detail,
                self.alive,
            )
        else:
            backtrace_dict = record_backtrace(self.gdb_node, self.size, backtrace_dict)


class Memdump(gdb.Command):
    """Dump the heap and mempool memory"""

    def __init__(self):
        super().__init__("memdump", gdb.COMMAND_USER)

    def mempool_dump(self, mpool, pid, seqmin, seqmax, address, simple, detail):
        """Dump the mempool memory"""
        for pool in mempool_multiple_foreach(mpool):
            if pid == PID_MM_FREE:
                for entry in NxSQueue(pool["queue"]):
                    gdb.write("%12u%#*x\n" % (pool["blocksize"], self.align, entry))
                    self.aordblks += 1
                    self.uordblks += mempool_realblocksize(pool)

                for entry in NxSQueue(pool["iqueue"]):
                    gdb.write("%12u%#*x\n" % (pool["blocksize"], self.align, entry))
                    self.aordblks += 1
                    self.uordblks += mempool_realblocksize(pool)
            else:
                if not mempool_backtrace_type:
                    return True

                for buf in mempool_foreach(pool):
                    if (
                        (pid == buf["pid"] or pid == PID_MM_ALLOC)
                        and (buf["seqno"] >= seqmin and buf["seqno"] < seqmax)
                        and buf["magic"] == MEMPOOL_MAGIC_ALLOC
                    ):
                        charnode = int(buf)
                        if detail:
                            mempool_dumpbuf(
                                buf,
                                pool["blocksize"],
                                1,
                                self.align,
                                simple,
                                detail,
                                check_node_alive(buf),
                            )
                        else:
                            self.backtrace_dict = record_backtrace(
                                buf, pool["blocksize"], self.backtrace_dict
                            )
                        if address and (
                            address < charnode
                            and address >= charnode - pool["blocksize"]
                        ):
                            mempool_dumpbuf(
                                buf,
                                pool["blocksize"],
                                1,
                                self.align,
                                simple,
                                detail,
                                check_node_alive(buf),
                            )
                            gdb.write(
                                "\nThe address 0x%x found belongs to"
                                "the mempool node with base address 0x%x\n"
                                % (address, charnode)
                            )
                            print_node = "p *(struct mempool_backtrace_s *)0x%x" % (
                                charnode
                            )
                            gdb.write(print_node + "\n")
                            gdb.execute(print_node)
                            return True
                        self.aordblks += 1
                        self.uordblks += mempool_realblocksize(pool)
        return False

    def memnode_dump(self, node):
        self.aordblks += 1
        self.uordblks += node.size
        node.dump(
            detail=self.detail,
            simple=self.simple,
            align=self.align,
            backtrace_dict=self.backtrace_dict,
        )

    def memdump_tail(self, detail, simple):
        if not detail:
            output = [v for v in self.backtrace_dict.values()]
            output.sort(key=get_count, reverse=True)
            for node in output:
                if node["node"].type == mm_allocnode_type.pointer():
                    mm_dumpnode(
                        node["node"],
                        node["count"],
                        self.align,
                        simple,
                        detail,
                        check_node_alive(node),
                    )
                else:
                    mempool_dumpbuf(
                        node["node"],
                        node["size"],
                        node["count"],
                        self.align,
                        simple,
                        detail,
                        check_node_alive(node),
                    )

        gdb.write("%12s%12s\n" % ("Total Blks", "Total Size"))
        gdb.write("%12d%12d\n" % (self.aordblks, self.uordblks))

    def memdump(self, pid, seqmin, seqmax, address, simple, detail, biggest_top=30):
        """Dump the heap memory"""

        self.simple = simple
        self.detail = detail

        alloc_node = []
        free_node = []
        mempool_node = []

        heap = gdb.parse_and_eval("g_mmheap")
        if heap.type.has_key("mm_mpool"):
            if self.mempool_dump(
                heap["mm_mpool"], pid, seqmin, seqmax, address, simple, detail
            ):
                return

        prev_node = None

        for gdb_node in mm_foreach(heap):
            node = HeapNode(gdb_node)

            if prev_node:
                prev_node.nextfree = not node.alloc

            prev_node = node

            if not node.inside_sequence(seqmin, seqmax):
                continue

            if address:
                if node.contains_address(address):
                    gdb.write(
                        "\nThe address 0x%x found belongs to"
                        "the memory node with base address 0x%x\n"
                        % (address, node.base)
                    )
                    print_node = "p *(struct mm_allocnode_s *)0x%x" % (node.base)
                    gdb.write(print_node + "\n")
                    gdb.execute(print_node)
                    return

            if node.pid == PID_MM_MEMPOOL:
                mempool_node.append(node)
            elif node.alloc:
                alloc_node.append(node)
            else:
                free_node.append(node)

        title_dict = {
            PID_MM_MEMPOOL: "Dump mempool:\n",
            PID_MM_ALLOC: "Dump all used memory node info, use '\x1b[33;1m*\x1b[m' mark pid is not exist:\n",
            PID_MM_FREE: "Dump all free memory node info:\n",
            PID_MM_BIGGEST: f"Dump biggest allocated top {biggest_top}\n",
            PID_MM_ORPHAN: "Dump allocated orphan nodes\n",
        }

        if pid in title_dict.keys():
            title = title_dict[pid]
        elif pid >= 0:
            title = title_dict[PID_MM_ALLOC]
        else:
            title = "Dump unspecific\n"

        gdb.write(title)
        if not detail:
            gdb.write("%6s" % ("CNT"))
        gdb.write(
            "%6s%12s%12s%8s%8s%8s\n"
            % ("PID", "Size", "Sequence", str(self.align), "Address", "Callstack")
        )

        if pid == PID_MM_FREE:
            self.detail = True
            for node in free_node:
                self.memnode_dump(node)
        elif pid == PID_MM_ALLOC:
            for node in alloc_node:
                self.memnode_dump(node)
        elif pid == PID_MM_BIGGEST:
            sorted_alloc = sorted(alloc_node)[-biggest_top:]
            for node in sorted_alloc:
                self.memnode_dump(node)
        elif pid == PID_MM_ORPHAN:
            for node in alloc_node:
                if node.is_orphan():
                    self.memnode_dump(node)
        elif pid == PID_MM_MEMPOOL:
            for node in mempool_node:
                self.memnode_dump(node)
        elif pid >= 0:
            for node in alloc_node:
                if node.pid == pid:
                    self.memnode_dump(node)

        self.memdump_tail(detail, simple)

    def complete(self, text, word):
        return gdb.COMPLETE_SYMBOL

    def parse_arguments(self, argv):
        parser = argparse.ArgumentParser(description="memdump command")
        parser.add_argument("-p", "--pid", type=str, help="Thread PID, -1 for mempool")
        parser.add_argument("-a", "--addr", type=str, help="Query memory address")
        parser.add_argument("-i", "--min", type=str, help="Minimum value")
        parser.add_argument("-x", "--max", type=str, help="Maximum value")
        parser.add_argument("--used", action="store_true", help="Used flag")
        parser.add_argument("--free", action="store_true", help="Free flag")
        parser.add_argument("--biggest", action="store_true", help="biggest allocated")
        parser.add_argument("--top", type=str, help="biggest top n, default 30")
        parser.add_argument(
            "--orphan", action="store_true", help="orphan allocated(neighbor of free)"
        )
        parser.add_argument(
            "-d",
            "--detail",
            action="store_true",
            help="Output details of each node",
            default=False,
        )
        parser.add_argument(
            "-s",
            "--simple",
            action="store_true",
            help="Simplified Output",
            default=False,
        )

        if argv[0] == "":
            argv = None
        try:
            args = parser.parse_args(argv)
        except SystemExit:
            return None

        return {
            "pid": int(args.pid, 0) if args.pid else None,
            "seqmin": int(args.min, 0) if args.min else 0,
            "seqmax": int(args.max, 0) if args.max else 0xFFFFFFFF,
            "used": args.used,
            "free": args.free,
            "addr": int(utils.parse_arg(args.addr)) if args.addr else None,
            "simple": args.simple,
            "detail": args.detail,
            "biggest": args.biggest,
            "orphan": args.orphan,
            "top": int(args.top) if args.top else 30,
        }

    def invoke(self, args, from_tty):
        if sizeof_size_t == 4:
            self.align = 11
        else:
            self.align = 19

        arg = self.parse_arguments(args.split(" "))

        if arg is None:
            return

        pid = PID_MM_ALLOC
        if arg["used"]:
            pid = PID_MM_ALLOC
        elif arg["free"]:
            pid = PID_MM_FREE
        elif arg["biggest"]:
            pid = PID_MM_BIGGEST
        elif arg["orphan"]:
            pid = PID_MM_ORPHAN
        elif arg["pid"]:
            pid = arg["pid"]
        if CONFIG_MM_BACKTRACE <= 0:
            arg["detail"] = True

        self.aordblks = 0
        self.uordblks = 0
        self.backtrace_dict = {}
        self.memdump(
            pid,
            arg["seqmin"],
            arg["seqmax"],
            arg["addr"],
            arg["simple"],
            arg["detail"],
            arg["top"],
        )


class Memleak(gdb.Command):
    """Memleak check"""

    def __init__(self):
        self.elf = utils.import_check(
            "elftools.elf.elffile", "ELFFile", "Plase pip install pyelftools\n"
        )
        if not self.elf:
            return

        super().__init__("memleak", gdb.COMMAND_USER)

    def next_ptr(self):
        inf = gdb.selected_inferior()
        heap = gdb.parse_and_eval("g_mmheap")
        longsize = get_long_type().sizeof
        region = get_symbol_value("CONFIG_MM_REGIONS")
        regions = []

        for i in range(0, region):
            start = int(heap["mm_heapstart"][i])
            end = int(heap["mm_heapend"][i])
            regions.append({"start": start, "end": end})

        # Search global variables
        for objfile in gdb.objfiles():
            gdb.write(f"Searching global symbol in: {objfile.filename}\n")
            elf = self.elf.load_from_path(objfile.filename)
            symtab = elf.get_section_by_name(".symtab")
            for symbol in symtab.iter_symbols():
                if symbol["st_info"]["type"] != "STT_OBJECT":
                    continue

                if symbol["st_size"] < longsize:
                    continue

                global_size = symbol["st_size"] // longsize * longsize
                global_mem = inf.read_memory(symbol["st_value"], global_size)
                while global_size:
                    global_size = global_size - longsize
                    ptr = read_ulong(global_mem, global_size)
                    for region in regions:
                        if ptr >= region["start"] and ptr < region["end"]:
                            yield ptr
                            break

        gdb.write("Searching in grey memory\n")
        for node in self.grey_list:
            addr = node["addr"]
            mem = inf.read_memory(addr, node["size"])
            i = 0
            while i < node["size"]:
                ptr = read_ulong(mem, i)
                for region in regions:
                    if ptr >= region["start"] and ptr < region["end"]:
                        yield ptr
                        break
                i = i + longsize

    def collect_white_dict(self):
        white_dict = {}
        allocnode_size = mm_allocnode_type.sizeof

        # collect all user malloc ptr

        heap = gdb.parse_and_eval("g_mmheap")
        for node in mm_foreach(heap):
            if node["size"] & MM_ALLOC_BIT != 0 and node["pid"] != PID_MM_MEMPOOL:
                addr = int(node) + allocnode_size

                node_dict = {}
                node_dict["node"] = node
                node_dict["size"] = mm_nodesize(node["size"]) - allocnode_size
                node_dict["addr"] = addr
                white_dict[int(addr)] = node_dict

        if heap.type.has_key("mm_mpool"):
            for pool in mempool_multiple_foreach(heap["mm_mpool"]):
                for buf in mempool_foreach(pool):
                    if buf["magic"] == MEMPOOL_MAGIC_ALLOC:
                        addr = int(buf) - pool["blocksize"]

                        buf_dict = {}
                        buf_dict["node"] = buf
                        buf_dict["size"] = pool["blocksize"]
                        buf_dict["addr"] = addr
                        white_dict[int(addr)] = buf_dict

        return white_dict

    def parse_arguments(self, argv):
        parser = argparse.ArgumentParser(description="memleak command")
        parser.add_argument(
            "-s",
            "--simple",
            action="store_true",
            help="Simplified Output",
            default=False,
        )
        parser.add_argument(
            "-d",
            "--detail",
            action="store_true",
            help="Output details of each node",
            default=False,
        )

        if argv[0] == "":
            argv = None
        try:
            args = parser.parse_args(argv)
        except SystemExit:
            return None

        return {"simple": args.simple, "detail": args.detail}

    def diagnose(self, *args, **kwargs):
        output = gdb.execute("memleak", to_string=True)
        return {
            "title": "Memory Leak Report",
            "command": "memleak",
            "details": output,
        }

    def invoke(self, args, from_tty):
        if sizeof_size_t == 4:
            align = 11
        else:
            align = 19

        arg = self.parse_arguments(args.split(" "))

        if arg is None:
            return

        if CONFIG_MM_BACKTRACE < 0:
            gdb.write("Need to set CONFIG_MM_BACKTRACE to 8 or 16 better.\n")
            return
        elif CONFIG_MM_BACKTRACE == 0:
            gdb.write("CONFIG_MM_BACKTRACE is 0, no backtrace available\n")

        start = last = time.time()
        white_dict = self.collect_white_dict()

        self.grey_list = []
        gdb.write("Searching for leaked memory, please wait a moment\n")
        last = time.time()

        sorted_keys = sorted(white_dict.keys())
        for ptr in self.next_ptr():
            # Find a closest addres in white_dict
            pos = bisect.bisect_right(sorted_keys, ptr)
            if pos == 0:
                continue
            grey_key = sorted_keys[pos - 1]
            if grey_key in white_dict and ptr < grey_key + white_dict[grey_key]["size"]:
                self.grey_list.append(white_dict[grey_key])
                del white_dict[grey_key]

        # All white node is leak

        gdb.write(f"Search all memory use {(time.time() - last):.2f} seconds\n")

        gdb.write("\n")
        if len(white_dict) == 0:
            gdb.write("All node have references, no memory leak!\n")
            return

        gdb.write("Leak catch!, use '\x1b[33;1m*\x1b[m' mark pid is not exist:\n")

        if CONFIG_MM_BACKTRACE > 0 and not arg["detail"]:
            gdb.write("%6s" % ("CNT"))

        gdb.write(
            "%6s%12s%12s%*s %s\n"
            % ("PID", "Size", "Sequence", align, "Address", "Callstack")
        )

        if CONFIG_MM_BACKTRACE > 0 and not arg["detail"]:

            # Filter same backtrace

            backtrace_dict = {}
            for addr in white_dict.keys():
                backtrace_dict = record_backtrace(
                    white_dict[addr]["node"], white_dict[addr]["size"], backtrace_dict
                )

            leaksize = 0
            leaklist = []
            for node in backtrace_dict.values():
                leaklist.append(node)

            # sort by count
            leaklist.sort(key=get_count, reverse=True)

            i = 0
            for node in leaklist:
                if node["node"].type == mm_allocnode_type.pointer():
                    mm_dumpnode(
                        node["node"],
                        node["count"],
                        align,
                        arg["simple"],
                        arg["detail"],
                        check_node_alive(node),
                    )
                else:
                    mempool_dumpbuf(
                        node["node"],
                        node["size"],
                        node["count"],
                        align,
                        arg["simple"],
                        arg["detail"],
                        check_node_alive(node),
                    )

                leaksize += node["count"] * node["size"]
                i += 1

            gdb.write(
                f"Alloc {len(white_dict)} count,\
have {i} some backtrace leak, total leak memory is {int(leaksize)} bytes\n"
            )
        else:
            leaksize = 0
            for node in white_dict.values():
                if node["node"].type == mm_allocnode_type.pointer():
                    mm_dumpnode(
                        node["node"],
                        1,
                        align,
                        arg["simple"],
                        True,
                        check_node_alive(node),
                    )
                else:
                    mempool_dumpbuf(
                        node["node"],
                        node["size"],
                        1,
                        align,
                        arg["simple"],
                        True,
                        check_node_alive(node),
                    )
                leaksize += node["size"]

            gdb.write(
                f"Alloc {len(white_dict)} count, total leak memory is {int(leaksize)} bytes\n"
            )

        gdb.write(f"Finished in {(time.time() - start):.2f} seconds\n")


class Memmap(gdb.Command):
    def __init__(self):
        self.np = utils.import_check("numpy", errmsg="Please pip install numpy\n")
        self.plt = utils.import_check(
            "matplotlib", "pyplot", errmsg="Please pip install matplotlib\n"
        )
        self.math = utils.import_check("math")
        if not self.np or not self.plt or not self.math:
            return

        super().__init__("memmap", gdb.COMMAND_USER)

    def save_memory_map(self, mallinfo, output_file):
        mallinfo = sorted(mallinfo, key=lambda item: item["addr"])
        start = mallinfo[0]["addr"]
        size = mallinfo[-1]["addr"] - start

        order = self.math.ceil(size**0.5)
        img = self.np.zeros([order, order])

        for node in mallinfo:
            addr = node["addr"]
            size = node["size"]
            start_index = addr - start
            end_index = start_index + size
            img.flat[start_index:end_index] = 1 + self.math.log2(node["sequence"] + 1)

        self.plt.imsave(output_file, img, cmap=self.plt.get_cmap("Greens"))

    def allocinfo(self):
        info = []
        heap = gdb.parse_and_eval("g_mmheap")
        for node in mm_foreach(heap):
            if node["size"] & MM_ALLOC_BIT != 0:
                allocnode = gdb.Value(node).cast(lookup_type("char").pointer())
                info.append(
                    {
                        "addr": int(allocnode),
                        "size": int(mm_nodesize(node["size"])),
                        "sequence": int(node["seqno"]),
                    }
                )
        return info

    def parse_arguments(self, argv):
        parser = argparse.ArgumentParser(description="memdump command")
        parser.add_argument(
            "-o", "--output", type=str, default="memmap", help="img output file"
        )
        if argv[0] == "":
            argv = None
        try:
            args = parser.parse_args(argv)
        except SystemExit:
            return None
        return args.output

    def invoke(self, args, from_tty):
        output_file = self.parse_arguments(args.split(" "))
        meminfo = self.allocinfo()
        self.save_memory_map(meminfo, output_file + ".png")


class Memfrag(gdb.Command):
    def __init__(self):
        super().__init__("memfrag", gdb.COMMAND_USER)

    def parse_arguments(self, argv):
        parser = argparse.ArgumentParser(description="memfrag command")
        parser.add_argument(
            "-d", "--detail", action="store_true", help="Output details"
        )
        if argv[0] == "":
            argv = None
        try:
            args = parser.parse_args(argv)
        except SystemExit:
            return None
        return args.detail

    def freeinfo(self):
        info = []
        heap = gdb.parse_and_eval("g_mmheap")
        for node in mm_foreach(heap):
            if node["size"] & MM_ALLOC_BIT == 0:
                freenode = gdb.Value(node).cast(lookup_type("char").pointer())
                info.append(
                    {
                        "addr": int(freenode),
                        "size": int(mm_nodesize(node["size"])),
                    }
                )
        return info

    def invoke(self, args, from_tty):
        detail = self.parse_arguments(args.split(" "))
        info = self.freeinfo()

        info = sorted(info, key=lambda item: item["size"], reverse=True)
        if detail:
            for node in info:
                gdb.write(f"addr: {node['addr']}, size: {node['size']}\n")

        heapsize = gdb.parse_and_eval("*g_mmheap")["mm_heapsize"]
        freesize = sum(node["size"] for node in info)
        remaining = freesize
        fragrate = 0

        for node in info:
            fragrate += (1 - (node["size"] / remaining)) * (node["size"] / freesize)
            remaining -= node["size"]

        fragrate = fragrate * 1000
        gdb.write(f"memory fragmentation rate: {fragrate:.2f}\n")
        gdb.write(
            f"heap size: {heapsize}, free size: {freesize}, uordblks:"
            f"{info.__len__()} largest block: {info[0]['size']} \n"
        )


class MempoolProc:
    def __init__(self, entry):
        pool = utils.container_of(entry, mempool_s_type, "procfs")
        blocksize = self.real_blocksize(pool)
        ordblks = sq_count(pool["queue"])
        iordblks = sq_count(pool["iqueue"])
        aordblks = int(pool["nalloc"])
        narena = sq_count(pool["equeue"])
        name = str(entry["name"])
        if '"' in name:
            # strip the name
            name_split = name.split('"')
            if len(name_split) == 3:
                name = name_split[1]
        self.pool = pool
        self.name = name
        self.arena = int(
            narena * sizeof_sq_entry_t + (aordblks + ordblks + iordblks) * blocksize
        )
        self.sizeblks = blocksize
        self.ordblks = ordblks
        self.iordblks = iordblks
        self.aordblks = aordblks
        if pool["wait"] and pool["expandsize"] == 0:
            self.nwaiter = -int(self.pool["waitsem"]["semcount"])
        else:
            self.nwaiter = 0

    def real_blocksize(self, pool) -> int:
        bsize = pool["blocksize"]
        if CONFIG_MM_BACKTRACE > 0:
            bsize = align_up(bsize + mempool_backtrace_type.sizeof, mempool_align())
        return int(bsize)

    def __str__(self) -> str:
        return self.__class__.format(
            self.name + ":",
            self.arena,
            self.sizeblks,
            self.aordblks,
            self.ordblks,
            self.iordblks,
            self.nwaiter,
        )

    @staticmethod
    def format(*args):
        return "{:>13}{:>11}{:>9}{:>9}{:>9}{:>9}{:>9}".format(*args)

    @staticmethod
    def get_title():
        return MempoolProc.format(
            "", "total", "bsize", "nused", "nfree", "nifree", "nwaiter"
        )


class Mempool(gdb.Command):
    def __init__(self):
        super().__init__("mempool", gdb.COMMAND_USER)

    def parse(self) -> dict:
        pools = {}
        try:
            entry = gdb.parse_and_eval("g_mempool_procfs")
        except gdb.error:
            return pools

        while entry:
            mempool = MempoolProc(entry)
            key = int(entry.address)
            if key in pools:
                raise ValueError(f"mempool deadloop {pools}")
            pools[key] = mempool
            entry = entry["next"]

        return pools

    def invoke(self, args, from_tty):
        pools = self.parse()
        if len(pools) == 0:
            gdb.write("mempool not found\n")
        else:
            gdb.write(f"{MempoolProc.get_title()}\n")
            for pool in pools.values():
                gdb.write(f"{pool}\n")
