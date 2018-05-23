# coding=utf-8
#
# This file is part of Hypothesis, which may be found at
# https://github.com/HypothesisWorks/hypothesis-python
#
# Most of this work is copyright (C) 2013-2018 David R. MacIver
# (david@drmaciver.com), but it contains contributions by others. See
# CONTRIBUTING.rst for a full list of people who may hold copyright, and
# consult the git log if you need to determine who owns an individual
# contribution.
#
# This Source Code Form is subject to the terms of the Mozilla Public License,
# v. 2.0. If a copy of the MPL was not distributed with this file, You can
# obtain one at http://mozilla.org/MPL/2.0/.
#
# END HEADER

from __future__ import division, print_function, absolute_import

from functools import reduce
from itertools import chain

import attr

import hypothesis.strategies as st
from hypothesis.errors import ResolutionFailed
from hypothesis.internal.compat import string_types, get_type_hints
from hypothesis.utils.conventions import infer


def from_attrs(target, args, kwargs, to_infer):
    """An internal version of builds(), specialised for Attrs classes."""
    fields = attr.fields(target)
    kwargs = {k: v for k, v in kwargs.items() if v is not infer}
    for name in to_infer:
        kwargs[name] = from_attrs_attribute(getattr(fields, name))
    # We might make this strategy more efficient if we added a layer here that
    # retries drawing if validation fails, for improved composition.
    # The treatment of timezones in datetimes() provides a precedent.
    return st.tuples(st.tuples(*args), st.fixed_dictionaries(kwargs)).map(
        lambda value: target(*value[0], **value[1])
    )


def from_attrs_attribute(attrib):
    """Infer a strategy from the metadata on an attr.Attribute object."""
    # Try inferring from the default argument.  Note that this will only help
    # the user passed `infer` to builds() for this attribute, but in that case
    # we use it as the minimal example.
    default = st.nothing()
    if isinstance(attrib.default, attr.Factory):
        if not getattr(attrib.default, 'takes_self', False):  # new in 17.1
            default = st.builds(attrib.default.factory)
    elif attrib.default is not attr.NOTHING:
        default = st.just(attrib.default)

    # Try inferring None, exact values, or type from attrs provided validators.
    null = st.nothing()  # updated to none() on seeing an OptionalValidator
    in_collections = []  # list of in_ validator collections to sample from
    validator_types = set()  # type constraints to pass to types_to_strategy()
    if attrib.validator is not None:
        validator = attrib.validator
        if isinstance(validator, attr.validators._OptionalValidator):
            null = st.none()
            validator = validator.validator
        if isinstance(validator, attr.validators._AndValidator):
            vs = validator._validators
        else:
            vs = [validator]
        for v in vs:
            if isinstance(v, attr.validators._InValidator):
                if isinstance(v.options, string_types):
                    in_collections.append(list(all_substrings(v.options)))
                else:
                    in_collections.append(v.options)
            elif isinstance(v, attr.validators._InstanceOfValidator):
                validator_types.add(v.type)

    # This is the important line.  We compose the final strategy from various
    # parts.  The default value, if any, is the minimal shrink, followed by
    # None (again, if allowed).  We then prefer to sample from values passed
    # to an in_ validator if available, but infer from a type otherwise.
    # Pick one because (sampled_from((1, 2)) | from_type(int)) would usually
    # fail validation by generating e.g. zero!
    if in_collections:
        sample = st.sampled_from(list(ordered_intersection(in_collections)))
        strat = default | null | sample
    else:
        strat = default | null | types_to_strategy(attrib, validator_types)

    # Better to give a meaningful error here than an opaque "could not draw"
    # when we try to get a value but have lost track of where this was created.
    if strat.is_empty:
        raise ResolutionFailed(
            'Cannot infer a strategy from the default, vaildator, type, or '
            'converter for %r' % (attrib,))
    return strat


def types_to_strategy(attrib, types):
    """Find all the type metadata for this attribute, reconcile it, and infer a
    strategy from the mess."""
    # If we know types from the validator(s), that's sufficient.
    if len(types) == 1:
        typ, = types
        if isinstance(typ, tuple):
            return st.one_of(*map(st.from_type, typ))
        return st.from_type(typ)
    elif types:
        # Multiple type validators.  Pick common type(s) that satisfy all.
        # TODO: use a more sophisticated aggregation that understands subtypes,
        # generic types, etc.  Consider pulling some code out of st.from_type?
        type_tuples = [k if isinstance(k, tuple) else (k,) for k in types]
        return st.one_of(*map(st.from_type, ordered_intersection(type_tuples)))

    # Otherwise, try the `type` attribute as a fallback, and finally try
    # the type hints on a converter (desperate!) before giving up.
    if isinstance(getattr(attrib, 'type', None), type):
        # The convoluted test is because variable annotations may be stored
        # in string form, and pass through attrs unevaluated.
        # See PEP 526, PEP 563, and Hypothesis issue #1004 for details.
        return st.from_type(attrib.type)

    converter = getattr(attrib, 'converter', None)
    if isinstance(converter, type):
        return st.from_type(converter)
    elif callable(converter):
        hints = get_type_hints(converter)
        if 'return' in hints:
            return st.from_type(hints['return'])

    return st.nothing()


def ordered_intersection(in_):
    """Set union of n sequences, ordered for reproducibility across runs."""
    intersection = reduce(set.intersection, in_, set(in_[0]))
    for x in chain.from_iterable(in_):
        if x in intersection:
            yield x
            intersection.remove(x)


def all_substrings(s):
    """Generate all substrings of `s`, in order of length then occurrence.
    Includes the empty string (first), and any duplicates that are present.

    >>> list(all_substrings('010'))
    ['', '0', '1', '0', '01', '10', '010']
    """
    yield s[:0]
    for n, _ in enumerate(s):
        for i in range(len(s) - n):
            yield s[i:i + n + 1]
