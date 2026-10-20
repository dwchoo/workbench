"""Recover a committed page image from fixed bytes, never a SQLite file name.

Only page framing is decoded here. SQLite validates SQL/schema/B-tree content
in memory. Format reference: https://www.sqlite.org/fileformat.html#walformat
"""
from __future__ import annotations

import struct

from .paths import DataDirError


def committed_image(main: bytes, wal: bytes = b'') -> tuple[bytes, dict]:
    if len(main)<100 or main[:16]!=b'SQLite format 3\0':
        raise DataDirError('migration SQLite main header is invalid')
    page_size=int.from_bytes(main[16:18],'big')
    if page_size==1:page_size=65536
    if not 512<=page_size<=65536 or page_size&(page_size-1) or len(main)%page_size:
        raise DataDirError('migration SQLite main page size/length is unsupported')
    if tuple(main[18:20]) not in ((1,1),(2,2)):
        raise DataDirError('migration SQLite main format is unsupported')
    main_pages=len(main)//page_size
    if not wal:
        image=bytearray(main);image[18]=image[19]=1
        return bytes(image),{'page_size':page_size,'committed_frames':0,'commit_pages':main_pages,
                            'tail':'none','journal_format':list(main[18:20])}
    if len(wal)<32:raise DataDirError('migration WAL header is incomplete')
    magic,version,wal_page,_,salt1,salt2,check1,check2=struct.unpack_from('>8I',wal)
    if magic not in (0x377f0682,0x377f0683) or version!=3007000:
        raise DataDirError('migration WAL magic/version is unsupported')
    if wal_page!=page_size or main[18:20]!=b'\x02\x02':
        raise DataDirError('migration main/WAL page size or format differs')
    order='<' if magic==0x377f0682 else '>'
    state=_checksum(memoryview(wal)[:24],(0,0),order)
    if state!=(check1,check2):raise DataDirError('migration WAL header checksum is invalid')
    frame_size=24+page_size
    complete=(len(wal)-32)//frame_size
    last_commit=0;commit_pages=main_pages;valid=0
    tail='partial-frame' if (len(wal)-32)%frame_size else 'complete'
    # Pass 1: validate only the contiguous current-epoch prefix. Never resync.
    for index in range(complete):
        offset=32+index*frame_size
        page,pages,s1,s2,c1,c2=struct.unpack_from('>6I',wal,offset)
        if (s1,s2)!=(salt1,salt2):tail='salt-epoch';break
        candidate=_checksum(memoryview(wal)[offset:offset+8],state,order)
        candidate=_checksum(memoryview(wal)[offset+24:offset+frame_size],candidate,order)
        if candidate!=(c1,c2):tail='checksum';break
        # Earlier transactions may reference pages above the final shrunk size.
        # Their addresses never determine allocation; only the final commit does.
        if not 1<=page<=0xfffffffe or pages>0xfffffffe:
            raise DataDirError('migration WAL page address exceeds SQLite format bounds')
        state=candidate;valid=index+1
        if pages:last_commit=valid;commit_pages=pages
    if tail=='complete' and valid>last_commit:tail='uncommitted'
    if tail=='complete' and not last_commit:tail='no-commit'
    if commit_pages>main_pages+last_commit:
        raise DataDirError('migration WAL commit size exceeds available input coverage')
    # Pass 2: locate the latest committed version of each page. Uncommitted
    # frames never contribute coverage or content, including after a shrink.
    latest={}
    for index in range(last_commit):
        offset=32+index*frame_size;page=struct.unpack_from('>I',wal,offset)[0]
        if page<=commit_pages:latest[page]=offset+24
    if sum(main_pages<page<=commit_pages for page in latest)!=max(0,commit_pages-main_pages):
        raise DataDirError('migration WAL committed image has missing new pages')
    image=bytearray(commit_pages*page_size)
    copied=min(len(main),len(image));image[:copied]=main[:copied]
    for page,offset in latest.items():image[(page-1)*page_size:page*page_size]=wal[offset:offset+page_size]
    if (image[:16]!=b'SQLite format 3\0' or image[16:18]!=main[16:18]
            or tuple(image[18:20]) not in ((1,1),(2,2))):
        raise DataDirError('migration committed page-one header is invalid')
    journal_format=list(image[18:20])
    image[18]=image[19]=1
    return bytes(image),{'page_size':page_size,'committed_frames':last_commit,
        'valid_frames':valid,'commit_pages':commit_pages,'tail':tail,'journal_format':journal_format}


def _checksum(data,state,order):
    first,second=state
    for left,right in struct.iter_unpack(order+'II',data):
        first=(first+left+second)&0xffffffff
        second=(second+right+first)&0xffffffff
    return first,second
