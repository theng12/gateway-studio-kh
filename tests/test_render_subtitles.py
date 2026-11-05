"""Finite synthetic subtitle fixtures; no renderer, fonts installed or network."""
from __future__ import annotations

import copy
import hashlib
import io
import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gateway.render_contract import SUBTITLE_CONTRACT, LATE_LABEL_CONTRACT, validate_render_packet_syntax
from gateway.render_subtitles import validate_subtitle_ass, validate_subtitle_font
from tests.test_render_late_labels import seal, legacy_packet, raw, label_op, asset_row

STYLE_FIELDS = 'Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding'


def ass(text=r'{\move(0,0,10,10)\kf20}Hello ខ្មែរ{\fn} reset'):
    return ('[Script Info]\nScriptType: v4.00+\nPlayResX: 1920\nPlayResY: 1080\n'
            '[V4+ Styles]\nFormat: ' + STYLE_FIELDS + '\n'
            'Style: Default,Fixture Family,40,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,1,2,10,10,10,1\n'
            '[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n'
            'Dialogue: 0,0:00:00.00,0:00:02.00,Default,,0,0,0,,' + text + '\n').encode()


def font(family='Fixture Family'):
    name = family.encode('utf-16-be')
    table = struct.pack('>HHH', 0, 1, 18) + struct.pack('>HHHHHH', 3, 1, 0x409, 1, len(name), 0) + name
    return (b'\0\1\0\0' + struct.pack('>HHHH', 1, 16, 0, 0)
            + b'name' + struct.pack('>III', 0, 28, len(table)) + table)


def operation():
    return {'kind': 'subtitle_stage', 'contract': SUBTITLE_CONTRACT,
            'ass': {'type': 'asset_ref', 'logical_id': 'subtitle_ass'},
            'fonts': [{'asset': {'type': 'asset_ref', 'logical_id': 'subtitle_font_0000'},
                       'family': 'Fixture Family', 'sha256': hashlib.sha256(font()).hexdigest()}]}


def packet():
    value = legacy_packet()
    value['recipe']['operations'] = [operation()]
    for name, role, suffix, data in [('subtitle_ass', 'subtitle_ass', 'ass', ass()),
            ('subtitle_font_0000', 'subtitle_font', 'ttf', font())]:
        value['manifest'].append({'logical_id': name, 'safe_name': name + '.' + suffix,
            'role': role, 'sha256': hashlib.sha256(data).hexdigest(), 'size_bytes': len(data)})
    return seal(value)


class SubtitlePacketTests(unittest.TestCase):
    def check(self, value, contract=SUBTITLE_CONTRACT):
        return validate_render_packet_syntax(raw(seal(value)), contract=contract)

    def test_exact_packet_and_combined_late_label_order(self):
        value = packet()
        self.assertEqual(self.check(value), value)
        value['recipe']['operations'].append(label_op(index=1))
        value['manifest'].append(asset_row('cue_0001_label'))
        self.check(value)
        value['recipe']['operations'].reverse()
        with self.assertRaises(ValueError):
            self.check(value)

    def test_subtitle_stage_cannot_use_legacy_or_late_only_contract(self):
        for contract in (None, LATE_LABEL_CONTRACT):
            with self.assertRaises(ValueError):
                self.check(packet(), contract)

    def test_closed_fonts_and_manifest_binding(self):
        for change in ('empty', 'hash', 'family', 'id', 'extra', 'singleton', 'extension'):
            value = packet()
            op = value['recipe']['operations'][0]
            if change == 'empty': op['fonts'] = []
            if change == 'hash': op['fonts'][0]['sha256'] = 'f' * 64
            if change == 'family': op['fonts'][0]['family'] = '/private/font.ttf'
            if change == 'id': op['fonts'][0]['asset']['logical_id'] = 'subtitle_font_0001'
            if change == 'extra': op['fonts'][0]['path'] = '/private/font.ttf'
            if change == 'singleton': value['recipe']['operations'].append(copy.deepcopy(op))
            if change == 'extension': next(r for r in value['manifest'] if r['role'] == 'subtitle_ass')['safe_name'] = 'subtitle_ass.ttf'
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.check(value)

    def test_new_extensions_cannot_expand_old_asset_roles(self):
        for contract in (None, LATE_LABEL_CONTRACT, SUBTITLE_CONTRACT):
            value = legacy_packet()
            value['manifest'][0]['safe_name'] = value['manifest'][0]['logical_id'] + '.ass'
            with self.assertRaises(ValueError):
                self.check(value, contract)


class SubtitleAssetTests(unittest.TestCase):
    def check_ass(self, data, **changes):
        options = dict(max_bytes=len(data), expected_sha256=hashlib.sha256(data).hexdigest(),
                       canvas={'width': 1920, 'height': 1080}, duration=3, families=['Fixture Family'])
        options.update(changes)
        validate_subtitle_ass(io.BytesIO(data), **options)

    def check_font(self, data, **changes):
        options = dict(max_bytes=len(data), expected_sha256=hashlib.sha256(data).hexdigest(), family='Fixture Family')
        options.update(changes)
        validate_subtitle_font(io.BytesIO(data), **options)

    def test_unicode_movement_karaoke_and_font_reset_survive(self):
        self.check_ass(ass())
        self.check_ass(ass(r'{\fnFixture Family\pos(3,4)\t(0,200,\bord4)}Hello'))
        self.check_font(font())

    def test_hash_and_read_ceiling_are_enforced(self):
        for check, data in ((self.check_ass, ass()), (self.check_font, font())):
            for options in ({'max_bytes': len(data) - 1}, {'expected_sha256': '0' * 64}):
                with self.subTest(options=options), self.assertRaises(ValueError):
                    check(data, **options)

    def test_external_payloads_and_invalid_encoding_refuse(self):
        for data in (ass() + b'[Fonts]\nfontname: x\n', ass() + b'[Graphics]\n',
                     ass().replace(b'ScriptType:', b'Include:'), b'\xff', ass() + b'\0',
                     ass().replace(b'[Events]', b'[Unknown]')):
            with self.subTest(data=data[:30]), self.assertRaises(ValueError): self.check_ass(data)

    def test_canvas_times_styles_and_font_closure_refuse(self):
        cases = [ass().replace(b'1920', b'1280'),
                 ass().replace(b'PlayResX: 1920', b'PlayResX: 1920\nPlayResX: 1920'),
                 ass().replace(b'0:00:02.00', b'0:00:04.00'),
                 ass().replace(b'0:00:00.00', b'0:00:02.50'),
                 ass().replace(b'Style: Default,', b'Style: Other,'),
                 ass(r'{\fnMissing}No font'),
                 ass().replace(b'Fontname,Fontsize', b'Fontname,Fontname'),
                 ass().replace(b'Family,40,', b'Family,nan,')]
        for data in cases:
            with self.subTest(data=data[:30]), self.assertRaises(ValueError): self.check_ass(data)
        with self.assertRaises(ValueError): self.check_ass(ass(), duration=float('nan'))

    def test_font_must_bind_a_real_name_table(self):
        cases = [b'not a font', font('Other Family'), font()[:27],
                 font().replace(b'name', b'head', 1)]
        for data in cases:
            with self.subTest(data=data[:30]), self.assertRaises(ValueError): self.check_font(data)

    def test_font_tables_cannot_overlap_header_or_each_other(self):
        data = bytearray(font())
        struct.pack_into('>I', data, 20, 0)
        with self.assertRaises(ValueError): self.check_font(bytes(data))
        data = bytearray(font())
        struct.pack_into('>H', data, 4, 2)
        data[28:28] = data[12:28]
        with self.assertRaises(ValueError): self.check_font(bytes(data))


if __name__ == '__main__': unittest.main()
