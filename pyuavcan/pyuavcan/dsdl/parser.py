#
# UAVCAN DSDL file parser
#
# Copyright (C) 2014 Pavel Kirienko <pavel.kirienko@gmail.com>
#

import os, re, logging
from .signature import compute_signature
from .common import DsdlException, pretty_filename
from .type_limits import get_unsigned_integer_range, get_signed_integer_range, get_float_range

MAX_FULL_TYPE_NAME_LEN = 80

class Type:
    CATEGORY_PRIMITIVE = 0
    CATEGORY_ARRAY = 1
    CATEGORY_COMPOUND = 2

    def __init__(self, full_name, category):
        self.full_name = str(full_name)
        self.category = category

    def __str__(self):
        return self.get_normalized_definition()

    __repr__ = __str__

class PrimitiveType(Type):
    KIND_BOOLEAN = 0
    KIND_UNSIGNED_INT = 1
    KIND_SIGNED_INT = 2
    KIND_FLOAT = 3

    CAST_MODE_SATURATED = 0
    CAST_MODE_TRUNCATED = 1

    def __init__(self, kind, bit_len, cast_mode):
        self.kind = kind
        self.bit_len = bit_len
        self.cast_mode = cast_mode
        super().__init__(self.get_normalized_definition(), Type.CATEGORY_PRIMITIVE)
        self.value_range = {
            PrimitiveType.KIND_BOOLEAN: get_unsigned_integer_range,
            PrimitiveType.KIND_UNSIGNED_INT: get_unsigned_integer_range,
            PrimitiveType.KIND_SIGNED_INT: get_signed_integer_range,
            PrimitiveType.KIND_FLOAT: get_float_range
        }[self.kind](bit_len)

    def get_normalized_definition(self):
        cast_mode = 'saturated' if self.cast_mode == PrimitiveType.CAST_MODE_SATURATED else 'truncated'
        primary_type = {
            PrimitiveType.KIND_BOOLEAN: 'bool',
            PrimitiveType.KIND_UNSIGNED_INT: 'uint' + str(self.bit_len),
            PrimitiveType.KIND_SIGNED_INT: 'int' + str(self.bit_len),
            PrimitiveType.KIND_FLOAT: 'float' + str(self.bit_len)
        }[self.kind]
        return cast_mode + ' ' + primary_type

    def validate_value_range(self, value):
        low, high = self.value_range
        if not low <= value <= high:
            _error('Value [%s] is out of range %s', value, self.value_range)

class ArrayType(Type):
    MODE_STATIC = 0
    MODE_DYNAMIC = 1

    def __init__(self, value_type, mode, max_size):
        self.value_type = value_type
        self.mode = mode
        self.max_size = max_size
        super().__init__(self.get_normalized_definition(), Type.CATEGORY_ARRAY)

    def get_normalized_definition(self):
        typedef = self.value_type.get_normalized_definition()
        return ('%s[<=%d]' if self.mode == ArrayType.MODE_DYNAMIC else '%s[%d]') % (typedef, self.max_size)

class CompoundType(Type):
    KIND_SERVICE = 0
    KIND_MESSAGE = 1

    def __init__(self, full_name, kind, dsdl_signature, dsdl_path, default_dtid):
        super().__init__(full_name, Type.CATEGORY_COMPOUND)
        self.dsdl_signature = dsdl_signature
        self.dsdl_path = dsdl_path
        self.default_dtid = default_dtid
        self.kind = kind
        if kind == CompoundType.KIND_SERVICE:
            self.request_fields = []
            self.response_fields = []
            self.request_constants = []
            self.response_constants = []
        elif kind == CompoundType.KIND_MESSAGE:
            self.fields = []
            self.constants = []
        else:
            _error('Compound type of unknown kind [%s]', kind)

    def get_normalized_definition(self):
        return self.full_name

    def get_normalized_attributes_definitions(self):
        def normdef(attrs):
            return '\n'.join([x.get_normalized_definition() for x in attrs])
        if self.kind == self.KIND_MESSAGE:
            return ('\n'.join([normdef(self.fields), normdef(self.constants)])).strip('\n')
        elif self.kind == self.KIND_SERVICE:
            rq = '\n'.join([normdef(self.request_fields), normdef(self.request_constants)])
            rs = '\n'.join([normdef(self.response_fields), normdef(self.response_constants)])
            return (rq + '\n---\n' + rs).strip('\n').replace('\n\n', '\n')


class Attribute:
    def __init__(self, type, name):  # @ReservedAssignment
        self.type = type
        self.name = name

    def __str__(self):
        return self.get_normalized_definition()

    __repr__ = __str__

class Field(Attribute):
    def get_normalized_definition(self):
        return '%s %s' % (self.type.get_normalized_definition(), self.name)

class Constant(Attribute):
    def __init__(self, type, name, init_expression, value):  # @ReservedAssignment
        super().__init__(type, name)
        self.init_expression = init_expression
        self.value = value
        self.string_value = repr(value)

    def get_normalized_definition(self):
        return '%s %s = %s' % (self.type.get_normalized_definition(), self.name, self.init_expression)


def _error(fmt, *args):
    raise DsdlException(fmt % args)

def _enforce(cond, fmt, *args):
    if not cond:
        _error(fmt, *args)

def evaluate_expression(expression):
    try:
        env = {
            'locals': None,
            'globals': None,
            '__builtins__': None,
            'true': 1,
            'false': 0
        }
        return eval(expression, env)
    except Exception as ex:
        _error('Cannot evaluate expression: %s', str(ex))

def validate_search_directories(dirnames):
    dirnames = set(dirnames)
    dirnames = list(map(os.path.abspath, dirnames))
    for d1 in dirnames:
        for d2 in dirnames:
            if d1 == d2:
                continue
            _enforce(not d1.startswith(d2), 'Nested search directories are not allowed [%s] [%s]', d1, d2)
            _enforce(d1.split(os.path.sep)[-1] != d2.split(os.path.sep)[-1],
                     'Namespace roots must be unique [%s] [%s]', d1, d2)
    return dirnames

def validate_namespace_name(name):
    for component in name.split('.'):
        _enforce(re.match(r'[a-z][a-z0-9_]*$', component), 'Invalid namespace name [%s]', name)
    _enforce(len(name) <= MAX_FULL_TYPE_NAME_LEN, 'Namespace name is too long [%s]', name)

def validate_compound_type_full_name(name):
    _enforce('.' in name, 'Full type name must explicitly specify its namespace [%s]', name)
    short_name = name.split('.')[-1]
    namespace = '.'.join(name.split('.')[:-1])
    validate_namespace_name(namespace)
    _enforce(re.match(r'[A-Z][A-Za-z0-9_]*$', short_name), 'Invalid type name [%s]', name)
    _enforce(len(name) <= MAX_FULL_TYPE_NAME_LEN, 'Type name is too long [%s]', name)

def validate_attribute_name(name):
    _enforce(re.match(r'[a-zA-Z][a-zA-Z0-9_]*$', name), 'Invalid attribute name [%s]', name)

def tokenize_dsdl_definition(text):
    for idx, line in enumerate(text.splitlines()):
        line = re.sub('#.*', '', line).strip()  # Remove comments and leading/trailing whitespaces
        if line:
            tokens = [tk for tk in line.split() if tk]
            yield idx + 1, tokens

class Parser:
    LOGGER_NAME = 'dsdl_parser'

    def __init__(self, search_dirs):
        self.search_dirs = validate_search_directories(search_dirs)
        self.log = logging.getLogger(Parser.LOGGER_NAME)

    def _namespace_from_filename(self, filename):
        search_dirs = sorted(map(os.path.abspath, self.search_dirs))  # Nested last
        filename = os.path.abspath(filename)
        for dirname in search_dirs:
            root_ns = dirname.split(os.path.sep)[-1]
            if filename.startswith(dirname):
                dir_len = len(dirname)
                basename_len = len(os.path.basename(filename))
                ns = filename[dir_len:-basename_len]
                ns = (root_ns + '.' + ns.replace(os.path.sep, '.').strip('.')).strip('.')
                validate_namespace_name(ns)
                return ns
        _error('File [%s] was not found in search directories', filename)

    def _full_typename_and_dtid_from_filename(self, filename):
        basename = os.path.basename(filename)
        items = basename.split('.')
        if (len(items) != 2 and len(items) != 3) or items[-1] != 'uavcan':
            _error('Invalid file name [%s]; expected pattern: [<default-dtid>.]<short-type-name>.uavcan', basename)
        if len(items) == 2:
            default_dtid, name = None, items[0]
        else:
            default_dtid, name = items[0], items[1]
            try:
                default_dtid = int(default_dtid)
            except ValueError:
                _error('Invalid default data type ID [%s]', default_dtid)
        full_name = self._namespace_from_filename(filename) + '.' + name
        validate_compound_type_full_name(full_name)
        return full_name, default_dtid

    def _compute_dsdl_signature(self, full_typename, fields, response_fields=None):
        text = full_typename + '\n'
        text += '\n'.join([x.get_normalized_definition() for x in fields])
        if response_fields is not None:
            text += '\n---\n'
            text += '\n'.join([x.get_normalized_definition() for x in response_fields])
        text = text.replace('\n\n', '\n')

        self.log.debug('DSDL signature of [%s] will be computed from:', full_typename)
        for ln in text.splitlines():
            self.log.debug('    %s', ln)

        return compute_signature(text)

    def _locate_compound_type_definition(self, referencing_filename, typename):
        def locate_namespace_directory(namespace):
            root_namespace, *sub_namespace_components = namespace.split('.')
            for directory in self.search_dirs:
                if directory.split(os.path.sep)[-1] == root_namespace:
                    return os.path.join(directory, *sub_namespace_components)
            _error('Unknown namespace [%s]', namespace)

        if '.' not in typename:
            current_namespace = self._namespace_from_filename(referencing_filename)
            full_typename = current_namespace + '.' + typename
        else:
            full_typename = typename
        namespace = '.'.join(full_typename.split('.')[:-1])
        directory = locate_namespace_directory(namespace)
        self.log.debug('Searching for [%s] in [%s]', full_typename, directory)

        for fn in os.listdir(directory):
            fn = os.path.join(directory, fn)
            if os.path.isfile(fn):
                try:
                    fn_full_typename, _dtid = self._full_typename_and_dtid_from_filename(fn)
                except Exception as ex:
                    self.log.info('Unknown file [%s], skipping... [%s]', pretty_filename(fn), ex)
                if full_typename == fn_full_typename:
                    return fn
        _error('Type definition not found [%s]', typename)

    def _parse_array_type(self, filename, value_typedef, size_spec, cast_mode):
        self.log.debug('Parsing the array value type [%s]...', value_typedef)
        value_type = self._parse_type(filename, value_typedef, cast_mode)
        _enforce(value_type.category != value_type.CATEGORY_ARRAY,
                 'Multidimensional arrays are not allowed (protip: use nested types)')
        try:
            if size_spec.startswith('<='):
                max_size = int(size_spec[2:], 0)
                mode = ArrayType.MODE_DYNAMIC
            elif size_spec.startswith('<'):
                max_size = int(size_spec[1:], 0) - 1
                mode = ArrayType.MODE_DYNAMIC
            else:
                max_size = int(size_spec, 0)
                mode = ArrayType.MODE_STATIC
        except ValueError:
            _error('Invalid array size specifier [%s] (valid patterns: [<=X], [<X], [X])', size_spec)
        _enforce(max_size > 0, 'Array size must be positive, not %d', max_size)
        return ArrayType(value_type, mode, max_size)

    def _parse_primitive_type(self, filename, base_name, bitlen, cast_mode):
        if cast_mode is None or cast_mode == 'saturated':
            cast_mode = PrimitiveType.CAST_MODE_SATURATED
        elif cast_mode == 'truncated':
            cast_mode = PrimitiveType.CAST_MODE_TRUNCATED
        else:
            _error('Invalid cast mode [%s]', cast_mode)

        if base_name == 'bool':
            return PrimitiveType(PrimitiveType.KIND_BOOLEAN, 1, cast_mode)
        try:
            kind = {
                'uint' : PrimitiveType.KIND_UNSIGNED_INT,
                'int'  : PrimitiveType.KIND_SIGNED_INT,
                'float': PrimitiveType.KIND_FLOAT,
            }[base_name]
        except KeyError:
            _error('Unknown primitive type (note: compound types should be in CamelCase)')

        if kind == PrimitiveType.KIND_FLOAT:
            _enforce(bitlen in (16, 32, 64), 'Invalid bit length for float type [%d]', bitlen)
        else:
            _enforce(2 <= bitlen <= 64, 'Invalid bit length [%d] (note: use bool instead of uint1)', bitlen)
        return PrimitiveType(kind, bitlen, cast_mode)

    def _parse_compound_type(self, filename, typedef):
        definition_filename = self._locate_compound_type_definition(filename, typedef)
        self.log.info('Nested type [%s] is defined in [%s], parsing...', typedef, pretty_filename(definition_filename))
        t = self.parse(definition_filename)
        if t.kind == t.KIND_SERVICE:
            _error('A service type can not be nested into another compound type')
        return t

    def _parse_type(self, filename, typedef, cast_mode):
        typedef = typedef.strip()
        array_match = re.match(r'(.+?)\[([^\]]*)\]$', typedef)
        primitive_match = re.match(r'([a-z]+)(\d{1,2})$|(bool)$', typedef)

        if array_match:
            assert not primitive_match
            value_typedef = array_match.group(1).strip()
            size_spec = array_match.group(2).strip()
            return self._parse_array_type(filename, value_typedef, size_spec, cast_mode)
        elif primitive_match:
            if primitive_match.group(0) == 'bool':
                return self._parse_primitive_type(filename, 'bool', 1, cast_mode)
            else:
                base_name = primitive_match.group(1)
                bitlen = int(primitive_match.group(2))
                return self._parse_primitive_type(filename, base_name, bitlen, cast_mode)
        else:
            _enforce(cast_mode is None, 'Cast mode specifier is not applicable for compound types [%s]', cast_mode)
            return self._parse_compound_type(filename, typedef)

    def _make_constant(self, attrtype, name, init_expression):
        _enforce(attrtype.category == attrtype.CATEGORY_PRIMITIVE, 'Invalid type for constant')
        value = evaluate_expression(init_expression)
        if not isinstance(value, (float, int, bool)):
            _error('Invalid type of constant initialization expression [%s]', type(value).__name__)
        value = {
            attrtype.KIND_UNSIGNED_INT : int,
            attrtype.KIND_SIGNED_INT : int,
            attrtype.KIND_BOOLEAN : int,  # Not bool because we need to check range
            attrtype.KIND_FLOAT : float
        }[attrtype.kind](value)
        self.log.debug('Constant initialization expression evaluated as: [%s] --> %s', init_expression, repr(value))
        attrtype.validate_value_range(value)
        return Constant(attrtype, name, init_expression, value)

    def _parse_line(self, filename, tokens):
        cast_mode = None
        if tokens[0] == 'saturated' or tokens[0] == 'truncated':
            cast_mode, *tokens = tokens

        if len(tokens) < 2:
            _error('Invalid attribute definition')

        typename, attrname, *tokens = tokens
        validate_attribute_name(attrname)
        attrtype = self._parse_type(filename, typename, cast_mode)

        if len(tokens) > 0:
            if len(tokens) < 2 or tokens[0] != '=':
                _error('Constant assignment expected')
            expression = ' '.join(tokens[1:])
            return self._make_constant(attrtype, attrname, expression)
        else:
            return Field(attrtype, attrname)

    def parse(self, filename):
        filename = os.path.abspath(filename)
        with open(filename) as f:
            text = f.read()
        try:
            full_typename, default_dtid = self._full_typename_and_dtid_from_filename(filename)
            numbered_lines = list(tokenize_dsdl_definition(text))
            all_attributes_names = set()
            fields, constants, resp_fields, resp_constants = [], [], [], []
            response_part = False
            for num, tokens in numbered_lines:
                if tokens == ['---']:
                    response_part = True
                    all_attributes_names = set()
                    continue
                try:
                    attr = self._parse_line(filename, tokens)
                    if attr.name in all_attributes_names:
                        _error('Duplicated attribute name [%s]', attr.name)
                    all_attributes_names.add(attr.name)
                    if isinstance(attr, Constant):
                        (resp_constants if response_part else constants).append(attr)
                    elif isinstance(attr, Field):
                        (resp_fields if response_part else fields).append(attr)
                    else:
                        _error('Unknown attribute type - internal error')
                except DsdlException as ex:
                    if not ex.line:
                        ex.line = num
                    raise ex
                except Exception as ex:
                    raise DsdlException('Internal error: %s' % str(ex), line=num) from ex

            if response_part:
                dsdl_signature = self._compute_dsdl_signature(full_typename, fields, resp_fields)
                typedef = CompoundType(full_typename, CompoundType.KIND_SERVICE, dsdl_signature, filename, default_dtid)
                typedef.request_fields = fields
                typedef.request_constants = constants
                typedef.response_fields = resp_fields
                typedef.response_constants = resp_constants
            else:
                dsdl_signature = self._compute_dsdl_signature(full_typename, fields)
                typedef = CompoundType(full_typename, CompoundType.KIND_MESSAGE, dsdl_signature, filename, default_dtid)
                typedef.fields = fields
                typedef.constants = constants

            self.log.info('Type [%s], default DTID [%s], signature [%08x], interpretation:',
                          full_typename, default_dtid, dsdl_signature)
            for ln in typedef.get_normalized_attributes_definitions().splitlines():
                self.log.info('    %s', ln)
            return typedef
        except DsdlException as ex:
            if not ex.file:
                ex.file = filename
            raise ex
        except Exception as ex:
            raise DsdlException('Internal error: %s' % str(ex), file=filename) from ex


if __name__ == '__main__':
    import sys
    logging.basicConfig(stream=sys.stderr, level=logging.DEBUG, format='%(levelname)s: %(message)s')

    if not sys.argv[1:]:
        self_directory = os.path.dirname(__file__)
        test_dir = os.path.join(self_directory, '..', '..', 'dsdl_test_data')
        test_dir = os.path.normpath(test_dir)
        parser = Parser([os.path.join(test_dir, 'root_a'), os.path.join(test_dir, 'root_b')])
        parser.log.setLevel(logging.DEBUG)
        t = parser.parse(os.path.join(test_dir, 'root_a', 'ns1', 'ns9', '425.BeginFirmwareUpdate.uavcan'))
    else:
        search_dirs = sys.argv[1:-1]
        filename = sys.argv[-1]
        parser = Parser(search_dirs)
        parser.log.setLevel(logging.DEBUG)
        t = parser.parse(filename)
