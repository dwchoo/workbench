"""Committed SQLite images: fixed rows and native recovery on separate copies."""
import sqlite3
from pathlib import Path
import tempfile
import struct
import unittest


class CommittedImageTests(unittest.TestCase):
    def reframe(self,wal,frames,big=False):
        header=bytearray(wal[:32]);header[:4]=(0x377f0683 if big else 0x377f0682).to_bytes(4,'big')
        byteorder='big' if big else 'little'
        total=[0,0]
        def add(block):
            for offset in range(0,len(block),8):
                total[0]=(total[0]+int.from_bytes(block[offset:offset+4],byteorder)+total[1])%2**32
                total[1]=(total[1]+int.from_bytes(block[offset+4:offset+8],byteorder)+total[0])%2**32
        add(header[:24]);header[24:]=struct.pack('>II',*total);output=header
        for content in frames:
            frame=bytearray(content);add(frame[:8]);add(frame[24:]);frame[16:24]=struct.pack('>II',*total)
            output+=frame
        return bytes(output)

    def wal_fixture(self,page_size=4096):
        with tempfile.TemporaryDirectory(prefix='wi53-wal-fixture-') as temp:
            path=Path(temp)/'original.db';db=sqlite3.connect(path)
            try:
                db.execute(f'PRAGMA page_size={page_size}');db.execute('PRAGMA journal_mode=WAL')
                db.execute('CREATE TABLE fixed(id INTEGER PRIMARY KEY,value TEXT)')
                db.executemany('INSERT INTO fixed VALUES (?,?)',[(1,'DELETE_ME'),(2,'OLD'),(3,'KEEP_MAIN')])
                db.commit();db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                db.execute("INSERT INTO fixed VALUES (4,'WAL_ONLY')");db.commit()
                db.execute("UPDATE fixed SET value='UPDATED_WAL' WHERE id=2")
                db.execute('DELETE FROM fixed WHERE id=1');db.commit()
                return path.read_bytes(),Path(str(path)+'-wal').read_bytes()
            finally:db.close()

    def rows(self,main,wal=b'',sql='SELECT id,value FROM fixed ORDER BY id'):
        with tempfile.TemporaryDirectory(prefix='wi53-native-control-') as temp:
            path=Path(temp)/'control.db';path.write_bytes(main)
            if wal:Path(str(path)+'-wal').write_bytes(wal)
            connection=sqlite3.connect(path)
            try:return connection.execute(sql).fetchall()
            finally:connection.close()

    def test_committed_wal_insert_update_delete_multiple_transactions_matches_native_copy(self):
        from workbench.backend.migration_sqlite import committed_image
        main,wal=self.wal_fixture()
        expected=[(2,'UPDATED_WAL'),(3,'KEEP_MAIN'),(4,'WAL_ONLY')]
        self.assertEqual(self.rows(main,wal),expected)
        image,receipt=committed_image(main,wal)
        self.assertEqual(self.rows(image),expected)
        self.assertGreater(receipt['committed_frames'],1)
        self.assertEqual(image[18:20],b'\x01\x01','deserialize image must use rollback mode')
        self.assertEqual(receipt['journal_format'],[2,2],'durable output must retain verified WAL mode')

    def test_checkpointed_shrink_can_retain_earlier_high_page_frames(self):
        from workbench.backend.migration_sqlite import committed_image
        with tempfile.TemporaryDirectory(prefix='wi53-shrink-') as temp:
            path=Path(temp)/'original.db';db=sqlite3.connect(path)
            try:
                db.execute('PRAGMA page_size=512');db.execute('PRAGMA journal_mode=WAL')
                db.execute('CREATE TABLE fixed(id INTEGER PRIMARY KEY,value TEXT)')
                db.execute("INSERT INTO fixed VALUES (7,'KEEP_THROUGH_SHRINK')")
                db.execute('CREATE TABLE large(id INTEGER PRIMARY KEY,value BLOB)')
                db.executemany('INSERT INTO large VALUES (?,?)',[(i,b'x'*800) for i in range(80)])
                db.commit();db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                db.execute('UPDATE large SET value=? WHERE id=79',(b'y'*800,));db.commit()
                db.execute('DROP TABLE large');db.commit();db.execute('VACUUM')
                db.execute('PRAGMA wal_checkpoint(FULL)')
                main=path.read_bytes();wal=Path(str(path)+'-wal').read_bytes()
                self.assertLess(len(main),4096)
            finally:db.close()
        size=512+24;frames=[wal[i:i+size] for i in range(32,len(wal)-size+1,size)]
        commits=[i for i,f in enumerate(frames) if int.from_bytes(f[4:8],'big')]
        last=commits[-1];start=commits[-2]+1 if len(commits)>1 else 0
        high=next(f for f in frames[:start] if int.from_bytes(f[:4],'big')>len(main)//512)
        # A checkpoint may already have truncated main while earlier valid WAL
        # history contains higher pages. Retain that obsolete frame plus the
        # complete final transaction; native recovery is the independent oracle.
        wal=self.reframe(wal,[high,*frames[start:last+1]])
        expected=[(7,'KEEP_THROUGH_SHRINK')]
        self.assertEqual(self.rows(main,wal),expected)
        image,receipt=committed_image(main,wal)
        self.assertEqual(self.rows(image),expected)
        self.assertEqual(len(image),receipt['commit_pages']*512)

    def test_only_contiguous_committed_prefix_survives_unfinished_or_invalid_tails(self):
        from workbench.backend.migration_sqlite import committed_image
        main,wal=self.wal_fixture();size=24+4096
        frames=[wal[i:i+size] for i in range(32,len(wal),size)]
        uncommitted=bytearray(frames[0]);uncommitted[4:8]=b'\0'*4
        with_uncommitted=self.reframe(wal,[*frames,uncommitted])
        valid_old_tail=self.reframe(wal,[*frames,frames[0]])[-size:]
        bad_checksum=bytearray(valid_old_tail);bad_checksum[-1]^=1
        old_epoch=bytearray(valid_old_tail);old_epoch[8]^=1
        variants={'uncommitted':with_uncommitted,'partial-frame':wal+valid_old_tail[:49],
                  'checksum':wal+bad_checksum+valid_old_tail,
                  'salt-epoch':wal+old_epoch+valid_old_tail}
        expected=[(2,'UPDATED_WAL'),(3,'KEEP_MAIN'),(4,'WAL_ONLY')]
        for tail,content in variants.items():
            with self.subTest(tail=tail):
                self.assertEqual(self.rows(main,content),expected)
                image,receipt=committed_image(main,content)
                self.assertEqual(self.rows(image),expected)
                self.assertEqual(receipt['committed_frames'],len(frames))
                self.assertEqual(receipt['tail'],tail)

    def test_both_checksum_word_orders_and_65536_pages_match_native_recovery(self):
        from workbench.backend.migration_sqlite import committed_image
        expected=[(2,'UPDATED_WAL'),(3,'KEEP_MAIN'),(4,'WAL_ONLY')]
        for page_size in (4096,65536):
            main,wal=self.wal_fixture(page_size);size=24+page_size
            frames=[wal[i:i+size] for i in range(32,len(wal),size)]
            for big in (False,True):
                with self.subTest(page_size=page_size,big=big):
                    content=self.reframe(wal,frames,big=big)
                    self.assertEqual(self.rows(main,content),expected)
                    image,receipt=committed_image(main,content)
                    self.assertEqual(self.rows(image),expected)
                    self.assertEqual(receipt['page_size'],page_size)

    def test_invalid_headers_and_uncovered_or_amplified_commits_refuse_before_allocation(self):
        from unittest.mock import patch
        from workbench.backend.migration_sqlite import committed_image
        from workbench.backend.paths import DataDirError
        main,wal=self.wal_fixture();size=4096+24;frame=wal[32:32+size]
        bad_header=bytearray(wal);bad_header[24]^=1
        unknown_version=bytearray(wal);unknown_version[4:8]=(3007001).to_bytes(4,'big')
        gap=bytearray(frame);gap[:4]=(4).to_bytes(4,'big');gap[4:8]=b'\0'*4
        gap_commit=bytearray(gap);gap_commit[4:8]=(4).to_bytes(4,'big')
        huge=bytearray(frame);huge[4:8]=(0xfffffffe).to_bytes(4,'big')
        zero=bytearray(frame);zero[:4]=b'\0'*4
        cases={'short-header':wal[:20],'header-checksum':bytes(bad_header),'version':bytes(unknown_version),
               'missing-page-coverage':self.reframe(wal,[gap,gap_commit]),
               'tiny-input-huge-commit':self.reframe(wal,[huge]),'zero-page':self.reframe(wal,[zero])}
        for name,content in cases.items():
            with self.subTest(name=name),patch('workbench.backend.migration_sqlite.bytearray',
                    side_effect=AssertionError('invalid input reached image allocation'),create=True):
                with self.assertRaises(DataDirError):committed_image(main,content)

    def test_wal_only_schema_fts_growth_and_partial_checkpoint_match_fixed_queries(self):
        from workbench.backend.migration_sqlite import committed_image
        with tempfile.TemporaryDirectory(prefix='wi53-growth-') as temp:
            path=Path(temp)/'original.db';db=sqlite3.connect(path)
            try:
                db.execute('PRAGMA page_size=512');db.execute('PRAGMA journal_mode=WAL')
                db.execute('CREATE TABLE fixed(id INTEGER PRIMARY KEY,value TEXT)')
                db.execute("INSERT INTO fixed VALUES (7,'BASE_SCHEMA')");db.commit()
                db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                db.execute('CREATE VIRTUAL TABLE search USING fts5(value)')
                db.executemany('INSERT INTO search(value) VALUES (?)',
                    [('delta searchable' if i==23 else 'alpha '*80,) for i in range(50)])
                db.commit();main=path.read_bytes();wal=Path(str(path)+'-wal').read_bytes()
            finally:db.close()
        partial=bytearray(main);size=512+24
        frame=next(wal[i:i+size] for i in range(32,len(wal),size)
                   if 0<int.from_bytes(wal[i:i+4],'big')<=len(main)//512)
        page=int.from_bytes(frame[:4],'big');partial[(page-1)*512:page*512]=frame[24:]
        query="SELECT rowid FROM search WHERE search MATCH 'delta' ORDER BY rowid"
        for original in (main,bytes(partial)):
            self.assertEqual(self.rows(original,wal,query),[(24,)])
            image,receipt=committed_image(original,wal)
            self.assertEqual(self.rows(image),[(7,'BASE_SCHEMA')])
            self.assertEqual(self.rows(image,sql=query),[(24,)])
            self.assertGreater(len(image),len(main))

    def test_main_only_image_reopens_in_memory_with_fixed_rows(self):
        from workbench.backend.migration_sqlite import committed_image
        original=sqlite3.connect(':memory:')
        try:
            original.execute('CREATE TABLE fixed(id INTEGER PRIMARY KEY,value TEXT)')
            original.execute("INSERT INTO fixed VALUES (7,'UNCHANGED_MAIN')")
            original.commit();main=original.serialize()
        finally:original.close()
        image,receipt=committed_image(main)
        reopened=sqlite3.connect(':memory:')
        try:
            reopened.deserialize(image)
            self.assertEqual(reopened.execute('SELECT id,value FROM fixed').fetchall(),[(7,'UNCHANGED_MAIN')])
            self.assertEqual(reopened.execute('PRAGMA integrity_check').fetchall(),[('ok',)])
        finally:reopened.close()
        self.assertEqual(image,main)
        self.assertEqual(receipt['committed_frames'],0)
        self.assertEqual(receipt['journal_format'],[1,1])

    def test_mixed_format_pairs_are_not_accepted_as_known_durable_modes(self):
        from workbench.backend.migration_sqlite import committed_image
        from workbench.backend.paths import DataDirError
        main,_=self.wal_fixture()
        for flags in ((1,2),(2,1),(0,0),(3,3)):
            with self.subTest(flags=flags):
                altered=bytearray(main);altered[18:20]=bytes(flags)
                with self.assertRaises(DataDirError):committed_image(bytes(altered))
