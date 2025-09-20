#!/bin/python3
import sqlite3
import snappy
import io
import sys
import glob
import pathlib
import re
import os
import json
import configparser
import platform




"""A SpiderMonkey StructuredClone object reader for Python."""
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
# Credits:
#   – Source was havily inspired by
#     https://dxr.mozilla.org/mozilla-central/rev/3bc0d683a41cb63c83cb115d1b6a85d50013d59e/js/src/vm/StructuredClone.cpp
#     and many helpful comments were copied as-is.
#   – Python source code by Alexander Schlarb, 2020.

import collections
import datetime
import enum
import io
import re
import struct
import typing


class ParseError(ValueError):
	pass


class InvalidHeaderError(ParseError):
	pass


class JSInt32(int):
	"""Type to represent the standard 32-bit signed integer"""
	def __init__(self, *a):
		if not (-0x80000000 <= self <= 0x7FFFFFFF):
			raise TypeError("JavaScript integers are signed 32-bit values")


class JSBigInt(int):
	"""Type to represent the arbitrary precision JavaScript “BigInt” type"""
	pass


class JSBigIntObj(JSBigInt):
	"""Type to represent the JavaScript BigInt object type (vs the primitive type)"""
	pass


class JSBooleanObj(int):
	"""Type to represent JavaScript boolean “objects” (vs the primitive type)
	
	Note: This derives from `int`, since one cannot directly derive from `bool`."""
	__slots__ = ()
	
	def __new__(self, inner: object = False):
		return int.__new__(bool(inner))
	
	def __and__(self, other: bool) -> bool:
		return bool(self) & other
	
	def __or__(self, other: bool) -> bool:
		return bool(self) | other
	
	def __xor__(self, other: bool) -> bool:
		return bool(self) ^ other
	
	def __rand__(self, other: bool) -> bool:
		return other & bool(self)
	
	def __ror__(self, other: bool) -> bool:
		return other | bool(self)
	
	def __rxor__(self, other: bool) -> bool:
		return other ^ bool(self)
	
	def __str__(self, other: bool) -> str:
		return str(bool(self))



class _HashableContainer:
	inner: object
	
	def __init__(self, inner: object):
		self.inner = inner
	
	def __hash__(self):
		return id(self.inner)
	
	def __repr__(self):
		return repr(self.inner)
	
	def __str__(self):
		return str(self.inner)


class JSMapObj(collections.UserDict):
	"""JavaScript compatible Map object that allows arbitrary values for the key."""
	@staticmethod
	def key_to_hashable(key: object) -> collections.abc.Hashable:
		try:
			hash(key)
		except TypeError:
			return _HashableContainer(key)
		else:
			return key
	
	def __contains__(self, key: object) -> bool:
		return super().__contains__(self.key_to_hashable(key))
	
	def __delitem__(self, key: object) -> None:
		return super().__delitem__(self.key_to_hashable(key))
	
	def __getitem__(self, key: object) -> object:
		return super().__getitem__(self.key_to_hashable(key))
	
	def __iter__(self) -> typing.Iterator[object]:
		for key in super().__iter__():
			if isinstance(key, _HashableContainer):
				key = key.inner
			yield key
	
	def __setitem__(self, key: object, value: object):
		super().__setitem__(self.key_to_hashable(key), value)


class JSNumberObj(float):
	"""Type to represent JavaScript number/float “objects” (vs the primitive type)"""
	pass


class JSRegExpObj:
	expr:  str
	flags: 'RegExpFlag'
	
	def __init__(self, expr: str, flags: 'RegExpFlag'):
		self.expr  = expr
		self.flags = flags
	
	@classmethod
	def from_re(cls, regex: re.Pattern) -> 'JSRegExpObj':
		flags = RegExpFlag.GLOBAL
		if regex.flags | re.DOTALL:
			pass  # Not supported in current (2020-01) version of SpiderMonkey
		if regex.flags | re.IGNORECASE:
			flags |= RegExpFlag.IGNORE_CASE
		if regex.flags | re.MULTILINE:
			flags |= RegExpFlag.MULTILINE
		return cls(regex.pattern, flags)
	
	def to_re(self) -> re.Pattern:
		flags = 0
		if self.flags | RegExpFlag.IGNORE_CASE:
			flags |= re.IGNORECASE
		if self.flags | RegExpFlag.GLOBAL:
			pass  # Matching type depends on matching function used in Python
		if self.flags | RegExpFlag.MULTILINE:
			flags |= re.MULTILINE
		if self.flags | RegExpFlag.UNICODE:
			pass  #XXX
		return re.compile(self.expr, flags)


class JSSavedFrame:
	def __init__(self):
		raise NotImplementedError()


class JSSetObj:
	def __init__(self):
		raise NotImplementedError()


class JSStringObj(str):
	"""Type to represent JavaScript string “objects” (vs the primitive type)"""
	pass



class DataType(enum.IntEnum):
	# Special values
	FLOAT_MAX = 0xFFF00000
	HEADER    = 0xFFF10000
	
	# Basic JavaScript types
	NULL      = 0xFFFF0000
	UNDEFINED = 0xFFFF0001
	BOOLEAN   = 0xFFFF0002
	INT32     = 0xFFFF0003
	STRING    = 0xFFFF0004
	
	# Extended JavaScript types
	DATE_OBJECT           = 0xFFFF0005
	REGEXP_OBJECT         = 0xFFFF0006
	ARRAY_OBJECT          = 0xFFFF0007
	OBJECT_OBJECT         = 0xFFFF0008
	ARRAY_BUFFER_OBJECT   = 0xFFFF0009
	BOOLEAN_OBJECT        = 0xFFFF000A
	STRING_OBJECT         = 0xFFFF000B
	NUMBER_OBJECT         = 0xFFFF000C
	BACK_REFERENCE_OBJECT = 0xFFFF000D
	#DO_NOT_USE_1
	#DO_NOT_USE_2
	TYPED_ARRAY_OBJECT    = 0xFFFF0010
	MAP_OBJECT            = 0xFFFF0011
	SET_OBJECT            = 0xFFFF0012
	END_OF_KEYS           = 0xFFFF0013
	#DO_NOT_USE_3
	DATA_VIEW_OBJECT      = 0xFFFF0015
	SAVED_FRAME_OBJECT    = 0xFFFF0016  # ?
	
	# Principals ?
	JSPRINCIPALS      = 0xFFFF0017
	NULL_JSPRINCIPALS = 0xFFFF0018
	RECONSTRUCTED_SAVED_FRAME_PRINCIPALS_IS_SYSTEM     = 0xFFFF0019
	RECONSTRUCTED_SAVED_FRAME_PRINCIPALS_IS_NOT_SYSTEM = 0xFFFF001A
	
	# ?
	SHARED_ARRAY_BUFFER_OBJECT = 0xFFFF001B
	SHARED_WASM_MEMORY_OBJECT  = 0xFFFF001C
	
	# Arbitrarily sized integers
	BIGINT        = 0xFFFF001D
	BIGINT_OBJECT = 0xFFFF001E
	
	# Older typed arrays
	TYPED_ARRAY_V1_MIN           = 0xFFFF0100
	TYPED_ARRAY_V1_INT8          = TYPED_ARRAY_V1_MIN + 0
	TYPED_ARRAY_V1_UINT8         = TYPED_ARRAY_V1_MIN + 1
	TYPED_ARRAY_V1_INT16         = TYPED_ARRAY_V1_MIN + 2
	TYPED_ARRAY_V1_UINT16        = TYPED_ARRAY_V1_MIN + 3
	TYPED_ARRAY_V1_INT32         = TYPED_ARRAY_V1_MIN + 4
	TYPED_ARRAY_V1_UINT32        = TYPED_ARRAY_V1_MIN + 5
	TYPED_ARRAY_V1_FLOAT32       = TYPED_ARRAY_V1_MIN + 6
	TYPED_ARRAY_V1_FLOAT64       = TYPED_ARRAY_V1_MIN + 7
	TYPED_ARRAY_V1_UINT8_CLAMPED = TYPED_ARRAY_V1_MIN + 8
	TYPED_ARRAY_V1_MAX           = TYPED_ARRAY_V1_UINT8_CLAMPED
	
	# Transfer-only tags (not used for persistent data)
	TRANSFER_MAP_HEADER              = 0xFFFF0200
	TRANSFER_MAP_PENDING_ENTRY       = 0xFFFF0201
	TRANSFER_MAP_ARRAY_BUFFER        = 0xFFFF0202
	TRANSFER_MAP_STORED_ARRAY_BUFFER = 0xFFFF0203


class RegExpFlag(enum.IntFlag):
	IGNORE_CASE = 0b00001
	GLOBAL      = 0b00010
	MULTILINE   = 0b00100
	UNICODE     = 0b01000


class Scope(enum.IntEnum):
	SAME_PROCESS                   = 1
	DIFFERENT_PROCESS              = 2
	DIFFERENT_PROCESS_FOR_INDEX_DB = 3
	UNASSIGNED                     = 4
	UNKNOWN_DESTINATION            = 5


class _Input:
	stream: io.BufferedReader
	
	def __init__(self, stream: io.BufferedReader):
		self.stream = stream
	
	def peek(self) -> int:
		try:
			return struct.unpack_from("<q", self.stream.peek(8))[0]
		except struct.error:
			raise EOFError() from None
	
	def peek_pair(self) -> (int, int):
		v = self.peek()
		return ((v >> 32) & 0xFFFFFFFF, (v >> 0) & 0xFFFFFFFF)
	
	def drop_padding(self, read_length):
		length = 8 - ((read_length - 1) % 8) - 1
		result = self.stream.read(length)
		if len(result) < length:
			raise EOFError()
	
	def read(self, fmt="q"):
		try:
			return struct.unpack("<" + fmt, self.stream.read(8))[0]
		except struct.error:
			raise EOFError() from None
	
	def read_bytes(self, length: int) -> bytes:
		result = self.stream.read(length)
		if len(result) < length:
			raise EOFError()
		self.drop_padding(length)
		return result
	
	def read_pair(self) -> (int, int):
		v = self.read()
		return ((v >> 32) & 0xFFFFFFFF, (v >> 0) & 0xFFFFFFFF)
	
	def read_double(self) -> float:
		return self.read("d")


class Reader:
	all_objs: typing.List[typing.Union[list, dict]]
	compat:   bool
	input:    _Input
	objs:     typing.List[typing.Union[list, dict]]
	
	
	def __init__(self, stream: io.BufferedReader):
		self.input = _Input(stream)
		
		self.all_objs = []
		self.compat   = False
		self.objs     = []
	
	
	def read(self):
		self.read_header()
		self.read_transfer_map()
		
		# Start out by reading in the main object and pushing it onto the 'objs'
		# stack. The data related to this object and its descendants extends
		# from here to the SCTAG_END_OF_KEYS at the end of the stream.
		add_obj, result = self.start_read()
		if add_obj:
			self.all_objs.append(result)
		
		# Stop when the stack shows that all objects have been read.
		while len(self.objs) > 0:
			# What happens depends on the top obj on the objs stack.
			obj = self.objs[-1]
			
			tag, data = self.input.peek_pair()
			if tag == DataType.END_OF_KEYS:
				# Pop the current obj off the stack, since we are done with it
				# and its children.
				self.input.read_pair()
				self.objs.pop()
				continue
			
			# The input stream contains a sequence of "child" values, whose
			# interpretation depends on the type of obj. These values can be
			# anything.
			#
			# startRead() will allocate the (empty) object, but note that when
			# startRead() returns, 'key' is not yet initialized with any of its
			# properties. Those will be filled in by returning to the head of
			# this loop, processing the first child obj, and continuing until
			# all children have been fully created.
			#
			# Note that this means the ordering in the stream is a little funky
			# for things like Map. See the comment above startWrite() for an
			# example.
			add_obj, key = self.start_read()
			if add_obj:
				self.all_objs.append(key)
			
			# Backwards compatibility: Null formerly indicated the end of
			# object properties.
			if key is None and not isinstance(obj, (JSMapObj, JSSetObj, JSSavedFrame)):
				self.objs.pop()
				continue
			
			# Set object: the values between obj header (from startRead()) and
			# DataType.END_OF_KEYS are interpreted as values to add to the set.
			if isinstance(obj, JSSetObj):
				obj.add(key)
			
			if isinstance(obj, JSSavedFrame):
				raise NotImplementedError()  #XXX: TODO
			
			# Everything else uses a series of key, value, key, value, … objects.
			add_obj, val = self.start_read()
			if add_obj:
				self.all_objs.append(val)
			
			# For a Map, store those <key,value> pairs in the contained map
			# data structure.
			if isinstance(obj, JSMapObj):
				obj[key] = value
			else:
				if not isinstance(key, (str, int)):
					#continue
					raise ParseError("JavaScript object key must be a string or integer")
				
				if isinstance(obj, list):
					# Ignore object properties on array
					if not isinstance(key, int) or key < 0:
						continue
					
					# Extend list with extra slots if needed
					while key >= len(obj):
						obj.append(NotImplemented)
				
				obj[key] = val
		
		self.all_objs.clear()
		
		return result
	
	
	def read_header(self) -> None:
		tag, data = self.input.peek_pair()
		
		scope: int
		if tag == DataType.HEADER:
			tag, data = self.input.read_pair()
			
			if data == 0:
				data = int(Scope.SAME_PROCESS)
			
			scope = data
		else:  # Old on-disk format
			scope = int(Scope.DIFFERENT_PROCESS_FOR_INDEX_DB)
		
		if scope == Scope.DIFFERENT_PROCESS:
			self.compat = False
		elif scope == Scope.DIFFERENT_PROCESS_FOR_INDEX_DB:
			self.compat = True
		elif scope == Scope.SAME_PROCESS:
			raise InvalidHeaderError("Can only parse persistent data")
		else:
			raise InvalidHeaderError("Invalid scope")
	
	
	def read_transfer_map(self) -> None:
		tag, data = self.input.peek_pair()
		if tag == DataType.TRANSFER_MAP_HEADER:
			raise InvalidHeaderError("Transfer maps are not allowed for persistent data")
	
	
	def read_bigint(self, info: int) -> JSBigInt:
		length   = info & 0x7FFFFFFF
		negative = bool(info & 0x80000000)
		raise NotImplementedError()
	
	
	def read_string(self, info: int) -> str:
		length = info & 0x7FFFFFFF
		latin1 = bool(info & 0x80000000)
		
		if latin1:
			return self.input.read_bytes(length).decode("latin-1")
		else:
			return self.input.read_bytes(length * 2).decode("utf-16le")
	
	
	def start_read(self):
		tag, data = self.input.read_pair()
		
		if tag == DataType.NULL:
			return False, None
		
		elif tag == DataType.UNDEFINED:
			return False, NotImplemented
		
		elif tag == DataType.INT32:
			if data > 0x7FFFFFFF:
				data -= 0x80000000
			return False, JSInt32(data)
		
		elif tag == DataType.BOOLEAN:
			return False, bool(data)
		elif tag == DataType.BOOLEAN_OBJECT:
			return True, JSBooleanObj(data)
		
		elif tag == DataType.STRING:
			return False, self.read_string(data)
		elif tag == DataType.STRING_OBJECT:
			return True, JSStringObj(self.read_string(data))
		
		elif tag == DataType.NUMBER_OBJECT:
			return True, JSNumberObj(self.input.read_double())
		
		elif tag == DataType.BIGINT:
			return False, self.read_bigint()
		elif tag == DataType.BIGINT_OBJECT:
			return True, JSBigIntObj(self.read_bigint())
		
		elif tag == DataType.DATE_OBJECT:
			# These timestamps are always UTC
			return True, datetime.datetime.fromtimestamp(self.input.read_double(),
			                                             datetime.timezone.utc)
		
		elif tag == DataType.REGEXP_OBJECT:
			flags = RegExpFlag(data)
			
			tag2, data2 = self.input.read_pair()
			if tag2 != DataType.STRING:
				#return False, False
				raise ParseError("RegExp type must be followed by string")
			
			return True, JSRegExpObj(flags, self.read_string(data2))
		
		elif tag == DataType.ARRAY_OBJECT:
			obj = []
			self.objs.append(obj)
			return True, obj
		elif tag == DataType.OBJECT_OBJECT:
			obj = {}
			self.objs.append(obj)
			return True, obj
		
		elif tag == DataType.BACK_REFERENCE_OBJECT:
			try:
				return False, self.all_objs[data]
			except IndexError:
				#return False, False
				raise ParseError("Object backreference to non-existing object") from None
		
		elif tag == DataType.ARRAY_BUFFER_OBJECT:
			return True, self.read_array_buffer(data)  #XXX: TODO
		
		elif tag == DataType.SHARED_ARRAY_BUFFER_OBJECT:
			return True, self.read_shared_array_buffer(data)  #XXX: TODO
		
		elif tag == DataType.SHARED_WASM_MEMORY_OBJECT:
			return True, self.read_shared_wasm_memory(data)  #XXX: TODO
		
		elif tag == DataType.TYPED_ARRAY_OBJECT:
			array_type = self.input.read()
			return False, self.read_typed_array(array_type, data)  #XXX: TODO
		
		elif tag == DataType.DATA_VIEW_OBJECT:
			return False, self.read_data_view(data)  #XXX: TODO
		
		elif tag == DataType.MAP_OBJECT:
			obj = JSMapObj()
			self.objs.append(obj)
			return True, obj
		
		elif tag == DataType.SET_OBJECT:
			obj = JSSetObj()
			self.objs.append(obj)
			return True, obj
		
		elif tag == DataType.SAVED_FRAME_OBJECT:
			obj = self.read_saved_frame(data)  #XXX: TODO
			self.objs.append(obj)
			return True, obj
		
		elif tag < int(DataType.FLOAT_MAX):
			# Reassemble double floating point value
			return False, struct.unpack("=d", struct.pack("=q", (tag << 32) | data))[0]
		
		elif DataType.TYPED_ARRAY_V1_MIN <= tag <= DataType.TYPED_ARRAY_V1_MAX:
			return False, self.read_typed_array(tag - DataType.TYPED_ARRAY_V1_MIN, data)
		
		else:
			#return False, False
			raise ParseError("Unsupported type")


















"""A parser for the Mozilla variant of Snappy frame format."""
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
# Credits:
#   – Python source code by Erin Yuki Schlarb, 2024.

import collections.abc as cabc
import io
import typing as ty

import cramjam


def decompress_raw(data: bytes) -> bytes:
	"""Decompress a raw Snappy chunk without any framing"""
	# Delegate this part to the cramjam library
	return cramjam.snappy.decompress_raw(data)


class Decompressor(io.BufferedIOBase):
	inner: io.BufferedIOBase
	
	_buf: bytearray
	_buf_len: int
	_buf_pos: int
	
	def __init__(self, inner: io.BufferedIOBase) -> None:
		assert inner.readable()
		self.inner = inner
		self._buf = bytearray(65536)
		self._buf_len = 0
		self._buf_pos = 0
	
	def readable(self) -> ty.Literal[True]:
		return True
	
	def _read_next_data_chunk(self) -> None:
		# We start with the buffer empty
		assert self._buf_len == 0
		
		# Keep parsing chunks until something is added to the buffer
		while self._buf_len == 0:
			# Read chunk header
			header = self.inner.read(4)
			if len(header) == 0:
				# EOF – buffer remains empty
				return
			elif len(header) != 4:
				# Just part of a header being present is invalid
				raise EOFError("Unexpected EOF while reading Snappy chunk header")
			type, length = header[0], int.from_bytes(header[1:4], "little")
			
			if type == 0xFF:
				# Stream identifier – contents should be checked but otherwise ignored
				if length != 6:
					raise ValueError("Invalid stream identifier (wrong length)")
				
				# Read and verify required content is present
				content = self.inner.read(length)
				if len(content) != 6:
					raise EOFError("Unexpected EOF while reading stream identifier")
				
				if content != b"sNaPpY":
					raise ValueError("Invalid stream identifier (wrong content)")
			elif type == 0x00:
				# Compressed data
				
				# Read checksum
				checksum: bytes = self.inner.read(4)
				if len(checksum) != 4:
					raise EOFError("Unexpected EOF while reading data checksum")
				
				# Read compressed data into new buffer
				compressed: bytes = self.inner.read(length - 4)
				if len(compressed) != length - 4:
					raise EOFError("Unexpected EOF while reading data contents")
				
				# Decompress data into inner buffer
				#XXX: There does not appear to an efficient way to set the length
				#     of a bytearray
				self._buf_len = cramjam.snappy.decompress_raw_into(compressed, self._buf)
				
				#TODO: Verify checksum
			elif type == 0x01:
				# Uncompressed data
				if length > 65536:
					raise ValueError("Invalid uncompressed data chunk (length > 65536)")
				
				checksum: bytes = self.inner.read(4)
				if len(checksum) != 4:
					raise EOFError("Unexpected EOF while reading data checksum")
				
				# Read chunk data into buffer
				with memoryview(self._buf) as view:
					if self.inner.readinto(view[:(length - 4)]) != length - 4:
						raise EOFError("Unexpected EOF while reading data contents")
					self._buf_len = length - 4
				
				#TODO: Verify checksum
			elif type in range(0x80, 0xFE + 1):
				# Padding and reserved skippable chunks – just skip the contents
				if self.inner.seekable():
					self.inner.seek(length, io.SEEK_CUR)
				else:
					self.inner.read(length)
			else:
				raise ValueError(f"Unexpected unskippable reserved chunk: 0x{type:02X}")
	
	def read1(self, size: ty.Optional[int] = -1) -> bytes:
		# Read another chunk if the buffer is currently empty
		if self._buf_len < 1:
			self._read_next_data_chunk()
		
		# Return some of the data currently present in the buffer
		start = self._buf_pos
		if size is None or size < 0:
			end = self._buf_len
		else:
			end = min(start + size, self._buf_len)
		
		result: bytes = bytes(self._buf[start:end])
		if end < self._buf_len:
			self._buf_pos = end
		else:
			self._buf_len = 0
			self._buf_pos = 0
		return result
	
	def read(self, size: ty.Optional[int] = -1) -> bytes:
		buf: bytearray = bytearray()
		if size is None or size < 0:
			while len(data := self.read1()) > 0:
				buf += data
		else:
			while len(buf) < size and len(data := self.read1(size - len(buf))) > 0:
				buf += data
		return buf
	
	def readinto1(self, buf: cabc.Sequence[bytes]) -> int:
		# Read another chunk if the buffer is currently empty
		if self._buf_len < 1:
			self._read_next_data_chunk()
		
		# Copy some of the data currently present in the buffer
		start = self._buf_pos
		end = min(start + len(buf), self._buf_len)
		
		buf[0:(end - start)] = self._buf[start:end]
		if end < self._buf_len:
			self._buf_pos = end
		else:
			self._buf_len = 0
			self._buf_pos = 0
		return end - start
	
	def readinto(self, buf: cabc.Sequence[bytes]) -> int:
		with memoryview(buf) as view:
			pos = 0
			while pos < len(buf) and (length := self.readinto1(view[pos:])) > 0:
				pos += length
			return pos


























def print_vaults(obj, f):
	if isinstance(obj, dict):
		if "vault" in obj:
			if obj["vault"] != "http://localhost":
				print("---------------------------------------")
				print("at:  ", f)
				print("Maybe found a Metamask vault:\n")
				print(obj["vault"])
				print("\n---------------------------------------\n\n\n")
		if "data" in obj and "salt" in obj:
			print("---------------------------------------")
			print("at:  ", f)
			print("Found a Metamask vault:\n")
			print(json.dumps(obj))
			print("\n---------------------------------------\n\n\n")
		for key in obj:
			print_vaults(obj[key])
	if isinstance(obj, str):
		if ("'data'" in obj or '"data"' in obj) and ("'salt'" in obj or '"salt"' in obj):
			print("---------------------------------------")
			print("at:  ", f)
			print("Probably found a Metamask vault:\n")
			print(obj)
			print("\n---------------------------------------\n\n\n")

def print_vaults_from_sqlite_file(f):
	try:
		with sqlite3.connect("file:" + f + "?mode=ro&immutable=1", uri=True) as conn:
			cur = conn.cursor()
			cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='object_data'")
			if len(cur.fetchall()) == 0:
				return
			cur.execute("SELECT * FROM object_data")
			rows = cur.fetchall()
			failures = 0
			for row in rows:
				try:
					decompressed = snappy.decompress(row[4])
				except AttributeError as ex:
					failures += 1
					if "'snappy' has no attribute" in str(ex):
						print("Failed to use python-snappy. Is it installed?")
						exit()
						os._exit(0)
					continue
				except:
					failures += 1
					continue
				
				try:
					reader = Reader(io.BufferedReader(io.BytesIO(decompressed)))
					content = reader.read()
				except:
					failures += 1
					continue
				
				print_vaults(content, f)

	except BaseException as ex:
		pass

SNAPPY_FRAMED_DATA_MAGIC_BYTES = bytes.fromhex("ff060000734e61507059")  # ....sNaPpY


def get_default_firefox_profile_paths():
	profile_dirs = set()
	home = pathlib.Path.home()
	system = platform.system()

	possible_roots = []
	if system == "Windows":
		appdata = os.environ.get("APPDATA")
		if appdata:
			possible_roots.append(pathlib.Path(appdata) / "Mozilla" / "Firefox")
	elif system == "Darwin":
		possible_roots.append(home / "Library" / "Application Support" / "Firefox")
	else:
		possible_roots.append(home / ".mozilla" / "firefox")

	for root in possible_roots:
		profiles_ini = root / "profiles.ini"
		if not profiles_ini.is_file():
			continue

		config = configparser.RawConfigParser()
		try:
			with profiles_ini.open("r", encoding="utf-8") as fh:
				config.read_file(fh)
		except (OSError, configparser.Error):
			continue

		def add_profile(path_value, is_relative=True):
			if not path_value:
				return

			candidate = pathlib.Path(path_value)
			if is_relative:
				candidate = root / candidate

			candidate = candidate.expanduser()
			try:
				candidate_resolved = candidate.resolve()
			except OSError:
				candidate_resolved = candidate

			if candidate_resolved.is_dir():
				profile_dirs.add(candidate_resolved)

		for section in config.sections():
			default_flag = config.get(section, "Default", fallback=None)

			if section.startswith("Profile"):
				if default_flag in ("1", "true", "True"):
					path_value = config.get(section, "Path", fallback=None)
					is_relative = True
					if config.has_option(section, "IsRelative"):
						try:
							is_relative = config.getboolean(section, "IsRelative")
						except ValueError:
							is_relative = config.get(section, "IsRelative") != "0"
					add_profile(path_value, is_relative)
			elif section.startswith("Install"):
				if default_flag:
					add_profile(default_flag, True)

	if not profile_dirs:
		for root in possible_roots:
			if not root.is_dir():
				continue
			for pattern in ("*.default*", "Profiles/*.default*"):
				for candidate in root.glob(pattern):
					if not candidate.is_dir():
						continue
					try:
						profile_dirs.add(candidate.resolve())
					except OSError:
						profile_dirs.add(candidate)

	return sorted(profile_dirs)


def scan_sqlite_files(base_path: pathlib.Path):
	for f in base_path.rglob('*.sqlite'):
		if not f.is_file():
			continue
		print_vaults_from_sqlite_file(str(f))


def scan_snappy_framed_files(base_path: pathlib.Path):
	for f in base_path.rglob('*'):
		if not f.is_file():
			continue

		try:
			with open(f, "rb") as ff:
				mb = ff.read(10)
		except OSError:
			continue

		if mb != SNAPPY_FRAMED_DATA_MAGIC_BYTES:
			continue

		try:
			with open(f, "rb") as ff:
				d = Decompressor(ff)
				decoded = d.read()
		except Exception:
			continue

		decodedStr = decoded.decode(encoding='utf-8', errors="ignore")

		pos = 0
		stop = False
		while not stop:
			# If the file does not contain "salt":" it definitely doesn't contains Metamask vault data.
			match = decodedStr.find('"salt":', pos)
			if match == -1:
				break

			# Continue scanning 5000 characters before the first "salt":"
			pos = max(pos, max(0, match - 5000))
			match = decodedStr.find("{", pos)
			if match == -1:
				break

			pos2 = pos
			while True:
				match2 = decodedStr.find("}", pos2)
				if match2 == -1:
					stop = True
					break
				if (match2 - match) > 10000:
					# { .... } string is too long to be metamask vault data
					pos = match2
					stop = True
					break
				snippet = decodedStr[match:(match2+1)]

				if "salt" not in snippet:
					pos2 = match2 + 1
				else:
					try:
						decodedSnippet = json.loads(snippet)
						if 'data' in decodedSnippet and 'salt' in decodedSnippet:
							print("---------------------------------------")
							print("at:  ", f)
							print("Found a Metamask vault:\n")
							print(snippet)
							print("\n---------------------------------------\n\n\n")
						pos = match2
						break
					except:
						#print("no decode")
						pos2 = match2 + 1
						continue


def scan_directory(base_path: pathlib.Path):
	base_path = base_path.expanduser()
	if not base_path.exists():
		return

	print(f"Scanning all .sqlite files in {base_path} recursively...")
	scan_sqlite_files(base_path)

	print("Looking for 'snappy framed data' files recursively...")
	scan_snappy_framed_files(base_path)


if len(sys.argv) >= 2:
	print_vaults_from_sqlite_file(sys.argv[1])
else:
	default_profiles = get_default_firefox_profile_paths()
	if default_profiles:
		for profile_path in default_profiles:
			print(f"Scanning Firefox profile: {profile_path}")
			scan_directory(profile_path)
	else:
		print("No default Firefox profile directories found. Scanning the current folder recursively...")
		scan_directory(pathlib.Path('.'))
