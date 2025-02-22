# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

import base64
import struct
from abc import ABC, abstractmethod
from functools import singledispatch
from typing import (
    Any,
    Callable,
    Generic,
    Literal,
    Optional,
    TypeVar,
)

import mmh3
from pydantic import Field, PositiveInt, PrivateAttr

from pyiceberg.types import (
    BinaryType,
    DateType,
    DecimalType,
    FixedType,
    IcebergType,
    IntegerType,
    LongType,
    StringType,
    TimestampType,
    TimestamptzType,
    TimeType,
    UUIDType,
)
from pyiceberg.utils import datetime
from pyiceberg.utils.decimal import decimal_to_bytes, truncate_decimal
from pyiceberg.utils.iceberg_base_model import IcebergBaseModel
from pyiceberg.utils.parsing import ParseNumberFromBrackets
from pyiceberg.utils.singleton import Singleton

S = TypeVar("S")
T = TypeVar("T")

IDENTITY = "identity"
VOID = "void"
BUCKET = "bucket"
TRUNCATE = "truncate"

BUCKET_PARSER = ParseNumberFromBrackets(BUCKET)
TRUNCATE_PARSER = ParseNumberFromBrackets(TRUNCATE)


class Transform(IcebergBaseModel, ABC, Generic[S, T]):
    """Transform base class for concrete transforms.

    A base class to transform values and project predicates on partition values.
    This class is not used directly. Instead, use one of module method to create the child classes.
    """

    __root__: str = Field()

    @classmethod
    def __get_validators__(cls):
        # one or more validators may be yielded which will be called in the
        # order to validate the input, each validator will receive as an input
        # the value returned from the previous validator
        yield cls.validate

    @classmethod
    def validate(cls, v: Any):
        # When Pydantic is unable to determine the subtype
        # In this case we'll help pydantic a bit by parsing the transform type ourselves
        if isinstance(v, str):
            if v == IDENTITY:
                return IdentityTransform()
            elif v == VOID:
                return VoidTransform()
            elif v.startswith(BUCKET):
                return BucketTransform(num_buckets=BUCKET_PARSER.match(v))
            elif v.startswith(TRUNCATE):
                return TruncateTransform(width=BUCKET_PARSER.match(v))
            else:
                return UnknownTransform(transform=v)
        return v

    @abstractmethod
    def transform(self, source: IcebergType) -> Callable[[Optional[S]], Optional[T]]:
        ...

    @abstractmethod
    def can_transform(self, source: IcebergType) -> bool:
        return False

    @abstractmethod
    def result_type(self, source: IcebergType) -> IcebergType:
        ...

    @property
    def preserves_order(self) -> bool:
        return False

    def satisfies_order_of(self, other) -> bool:
        return self == other

    def to_human_string(self, _: IcebergType, value: Optional[S]) -> str:
        return str(value) if value is not None else "null"

    @property
    def dedup_name(self) -> str:
        return self.__str__()

    def __str__(self) -> str:
        return self.__root__

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, Transform):
            return self.__root__ == other.__root__
        return False


class BucketTransform(Transform[S, int]):
    """Base Transform class to transform a value into a bucket partition value

    Transforms are parameterized by a number of buckets. Bucket partition transforms use a 32-bit
    hash of the source value to produce a positive value by mod the bucket number.

    Args:
      num_buckets (int): The number of buckets.
    """

    _source_type: IcebergType = PrivateAttr()
    _num_buckets: PositiveInt = PrivateAttr()

    def __init__(self, num_buckets: int, **data: Any):
        super().__init__(__root__=f"bucket[{num_buckets}]", **data)
        self._num_buckets = num_buckets

    @property
    def num_buckets(self) -> int:
        return self._num_buckets

    def hash(self, value: S) -> int:
        raise NotImplementedError()

    def apply(self, value: Optional[S]) -> Optional[int]:
        return (self.hash(value) & IntegerType.max) % self._num_buckets if value else None

    def result_type(self, source: IcebergType) -> IcebergType:
        return IntegerType()

    def can_transform(self, source: IcebergType) -> bool:
        return type(source) in {
            IntegerType,
            DateType,
            LongType,
            TimeType,
            TimestampType,
            TimestamptzType,
            DecimalType,
            StringType,
            FixedType,
            BinaryType,
            UUIDType,
        }

    def transform(self, source: IcebergType, bucket: bool = True) -> Callable[[Optional[Any]], Optional[int]]:
        source_type = type(source)
        if source_type in {IntegerType, LongType, DateType, TimeType, TimestampType, TimestamptzType}:

            def hash_func(v):
                return mmh3.hash(struct.pack("<q", v))

        elif source_type == DecimalType:

            def hash_func(v):
                return mmh3.hash(decimal_to_bytes(v))

        elif source_type in {StringType, FixedType, BinaryType}:

            def hash_func(v):
                return mmh3.hash(v)

        elif source_type == UUIDType:

            def hash_func(v):
                return mmh3.hash(
                    struct.pack(
                        ">QQ",
                        (v.int >> 64) & 0xFFFFFFFFFFFFFFFF,
                        v.int & 0xFFFFFFFFFFFFFFFF,
                    )
                )

        else:
            raise ValueError(f"Unknown type {source}")

        if bucket:
            return lambda v: (hash_func(v) & IntegerType.max) % self._num_buckets if v else None
        return hash_func

    def __repr__(self) -> str:
        return f"BucketTransform(num_buckets={self._num_buckets})"


def _base64encode(buffer: bytes) -> str:
    """Converts bytes to base64 string"""
    return base64.b64encode(buffer).decode("ISO-8859-1")


class IdentityTransform(Transform[S, S]):
    """Transforms a value into itself.

    Example:
        >>> transform = IdentityTransform()
        >>> transform.transform(StringType())('hello-world')
        'hello-world'
    """

    __root__: Literal["identity"] = Field(default="identity")
    _source_type: IcebergType = PrivateAttr()

    def transform(self, source: IcebergType) -> Callable[[Optional[S]], Optional[S]]:
        return lambda v: v

    def can_transform(self, source: IcebergType) -> bool:
        return source.is_primitive

    def result_type(self, source: IcebergType) -> IcebergType:
        return source

    @property
    def preserves_order(self) -> bool:
        return True

    def satisfies_order_of(self, other: Transform) -> bool:
        """ordering by value is the same as long as the other preserves order"""
        return other.preserves_order

    def to_human_string(self, source_type: IcebergType, value: Optional[S]) -> str:
        return _human_string(value, source_type) if value is not None else "null"

    def __str__(self) -> str:
        return "identity"

    def __repr__(self) -> str:
        return "IdentityTransform()"


class TruncateTransform(Transform[S, S]):
    """A transform for truncating a value to a specified width.
    Args:
      width (int): The truncate width, should be positive
    Raises:
      ValueError: If a type is provided that is incompatible with a Truncate transform
    """

    __root__: str = Field()
    _source_type: IcebergType = PrivateAttr()
    _width: PositiveInt = PrivateAttr()

    def __init__(self, width: int, **data: Any):
        super().__init__(__root__=f"truncate[{width}]", **data)
        self._width = width

    def can_transform(self, source: IcebergType) -> bool:
        return type(source) in {IntegerType, LongType, StringType, BinaryType, DecimalType}

    def result_type(self, source: IcebergType) -> IcebergType:
        return source

    @property
    def preserves_order(self) -> bool:
        return True

    @property
    def source_type(self) -> IcebergType:
        return self._source_type

    @property
    def width(self) -> int:
        return self._width

    def transform(self, source: IcebergType) -> Callable[[Optional[S]], Optional[S]]:
        source_type = type(source)
        if source_type in {IntegerType, LongType}:

            def truncate_func(v):
                return v - v % self._width

        elif source_type in {StringType, BinaryType}:

            def truncate_func(v):
                return v[0 : min(self._width, len(v))]

        elif source_type == DecimalType:

            def truncate_func(v):
                return truncate_decimal(v, self._width)

        else:
            raise ValueError(f"Cannot truncate for type: {source}")

        return lambda v: truncate_func(v) if v else None

    def satisfies_order_of(self, other: Transform) -> bool:
        if self == other:
            return True
        elif (
            isinstance(self.source_type, StringType)
            and isinstance(other, TruncateTransform)
            and isinstance(other.source_type, StringType)
        ):
            return self.width >= other.width

        return False

    def to_human_string(self, _: IcebergType, value: Optional[S]) -> str:
        if value is None:
            return "null"
        elif isinstance(value, bytes):
            return _base64encode(value)
        else:
            return str(value)

    def __repr__(self) -> str:
        return f"TruncateTransform(width={self._width})"


@singledispatch
def _human_string(value: Any, _type: IcebergType) -> str:
    return str(value)


@_human_string.register(bytes)
def _(value: bytes, _type: IcebergType) -> str:
    return _base64encode(value)


@_human_string.register(int)
def _(value: int, _type: IcebergType) -> str:
    return _int_to_human_string(_type, value)


@singledispatch
def _int_to_human_string(_type: IcebergType, value: int) -> str:
    return str(value)


@_int_to_human_string.register(DateType)
def _(_type: IcebergType, value: int) -> str:
    return datetime.to_human_day(value)


@_int_to_human_string.register(TimeType)
def _(_type: IcebergType, value: int) -> str:
    return datetime.to_human_time(value)


@_int_to_human_string.register(TimestampType)
def _(_type: IcebergType, value: int) -> str:
    return datetime.to_human_timestamp(value)


@_int_to_human_string.register(TimestamptzType)
def _(_type: IcebergType, value: int) -> str:
    return datetime.to_human_timestamptz(value)


class UnknownTransform(Transform):
    """A transform that represents when an unknown transform is provided
    Args:
      source_type (IcebergType): An Iceberg `Type`
      transform (str): A string name of a transform
    Raises:
      AttributeError: If the apply method is called.
    """

    __root__: Literal["unknown"] = Field(default="unknown")
    _source_type: IcebergType = PrivateAttr()
    _transform: str = PrivateAttr()

    def __init__(self, transform: str, **data: Any):
        super().__init__(**data)
        self._transform = transform

    def transform(self, source: IcebergType) -> Callable[[Optional[S]], Optional[T]]:
        raise AttributeError(f"Cannot apply unsupported transform: {self}")

    def can_transform(self, source: IcebergType) -> bool:
        return False

    def result_type(self, source: IcebergType) -> IcebergType:
        return StringType()

    def __repr__(self) -> str:
        return f"UnknownTransform(transform={repr(self._transform)})"


class VoidTransform(Transform, Singleton):
    """A transform that always returns None"""

    __root__ = "void"

    def transform(self, source: IcebergType) -> Callable[[Optional[S]], Optional[T]]:
        return lambda v: None

    def can_transform(self, _: IcebergType) -> bool:
        return True

    def result_type(self, source: IcebergType) -> IcebergType:
        return source

    def to_human_string(self, _: IcebergType, value: Optional[S]) -> str:
        return "null"

    def __repr__(self) -> str:
        return "VoidTransform()"
