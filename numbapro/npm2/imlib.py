from collections import defaultdict
import operator
import llvm.core as lc
from . import typesets, types, cgutils

class ImpLib(object):
    '''There is 1-to-1 correspondance between function declaration
    and function implementator.
    '''
    def __init__(self, funclib):
        self.funclib = funclib
        self.defnlib = {}

    def define(self, imp):
        defn = self.funclib.lookup(imp.funcobj, imp.args)

        if defn is None:
            msg = 'no matching definition for %s(%s)'
            raise TypeError(msg % (imp.funcobj, ', '.join(map(str, imp.args))))

        if imp.return_type is not None and defn.return_type != imp.return_type:
            msg = 'return-type mismatch for %s; got %s'
            raise TypeError(msg % (defn, imp.return_type))

        self.defnlib[defn] = imp

    def get(self, funcdef):
        return self.defnlib[funcdef]

    def lookup(self, funcobj, argtys):
        defn = self.funclib.lookup(funcobj, argtys)
        imp = self.get(defn)
        def wrap(context, args):
            return imp(context, args, argtys, defn.return_type)
        return wrap

    def populate_builtin(self):
        populate_builtin_impl(self)

class Imp(object):
    __slots__ = 'impl', 'funcobj', 'args', 'return_type', 'is_parametric'

    def __init__(self, impl, funcobj, args, return_type=None):
        self.impl = impl
        self.funcobj = funcobj
        self.args = args
        self.return_type = return_type
        self.is_parametric = ((return_type is None) or
                                any(callable(a) or isinstance(a, types.Kind)
                                    for a in args))

    def __call__(self, context, args, argtys, retty):
        if self.is_parametric:
            return self.impl(context, args, argtys, retty)
        else:
            return self.impl(context, args)

    def __repr__(self):
        return '<Impl %s %s -> %s >' % (self.funcobj, self.args,
                                        self.return_type)

def arith_int_overflow(context, sa, sb, ss):
    '''Arithmetic integer overflow when the signbit of the two operands are
    the same but the signbit of the result mismatches the signbit of the 
    operands.
    '''
    builder = context.builder

    # sa and sb have the same sign
    t1 = builder.not_(builder.xor(sa, sb))
    # sa (or sb) and ss have different sign
    t2 = builder.xor(sa, ss)

    overflow = builder.and_(t1, t2)
    
    # overflow
    with cgutils.if_then(builder, overflow):
        context.raises(OverflowError, "integer operation overflow")

# binary add

def imp_add_unsigned(context, args):
    a, b = args
    return context.builder.add(a, b)

def imp_add_signed(context, args):
    a, b = args
    sum = context.builder.add(a, b)
    if context.flags.overflow:
        sb = lambda x: types.signbit(context.builder, x)
        arith_int_overflow(context, sb(a), sb(b), sb(sum))
    return sum

def imp_add_float(context, args):
    a, b = args
    return context.builder.fadd(a, b)

def imp_add_complex(dtype):
    def imp(context, args):
        a, b = args

        a_real, a_imag = dtype.llvm_unpack(context.builder, a)
        b_real, b_imag = dtype.llvm_unpack(context.builder, b)

        c_real = imp_add_float(context, (a_real, b_real))
        c_imag = imp_add_float(context, (a_imag, b_imag))

        return dtype.desc.llvm_pack(context.builder, c_real, c_imag)
    return imp

# binary sub

def imp_sub_unsigned(context, args):
    a, b = args
    return context.builder.sub(a, b)

def imp_sub_signed(context, args):
    a, b = args
    diff = context.builder.sub(a, b)
    if context.flags.overflow:
        sb = lambda x: types.signbit(context.builder, x)
        arith_int_overflow(context, sb(a), context.builder.not_(sb(b)),
                           sb(diff))
    return diff

def imp_sub_float(context, args):
    a, b = args
    return context.builder.fsub(a, b)

def imp_sub_complex(dtype):
    def imp(context, args):
        a, b = args

        a_real, a_imag = dtype.llvm_unpack(context.builder, a)
        b_real, b_imag = dtype.llvm_unpack(context.builder, b)

        c_real = imp_sub_float(context, (a_real, b_real))
        c_imag = imp_sub_float(context, (a_imag, b_imag))

        return dtype.desc.llvm_pack(context.builder, c_real, c_imag)
    return imp

# binary mul

def imp_mul_unsigned(context, args):
    a, b = args
    return context.builder.mul(a, b)

def imp_mul_signed(context, args):
    a, b = args
    res = context.builder.mul(a, b)
    if context.flags.overflow:
        # NOTE: Does not catch all cases, but helps a little in debugging.
        sb = lambda x: types.signbit(context.builder, x)
        builder = context.builder
        diffsign = builder.xor(sb(a), sb(b))
        # different sign --> negative result
        # same sign --> positive result
        of = builder.xor(sb(res), diffsign)
        with cgutils.if_then(builder, of):
            context.raises(OverflowError, "signed multiply overflow")
    return res

def imp_mul_float(context, args):
    a, b = args
    return context.builder.fmul(a, b)

def imp_mul_complex(dtype):
    '''
    x y = (a c - b d) + i (a d + b c)
    '''
    def imp(context, args):
        x, y = args

        a, b = dtype.llvm_unpack(context.builder, x)
        c, d = dtype.llvm_unpack(context.builder, y)

        ac = imp_mul_float(context, (a, c))
        bd = imp_mul_float(context, (b, d))
        ad = imp_mul_float(context, (a, d))
        bc = imp_mul_float(context, (b, c))

        real = imp_sub_float(context, (ac, bd))
        imag = imp_add_float(context, (ad, bc))

        return dtype.desc.llvm_pack(context.builder, real, imag)
    return imp

# binary floordiv

def zero_division_check(context, divisor):
    if context.flags.zerodivision:
        builder = context.builder
        if divisor.type.kind != lc.TYPE_INTEGER:
            comparor = lambda x, y: builder.fcmp(lc.FCMP_OEQ, x, y)
        else:
            comparor = lambda x, y: builder.icmp(lc.ICMP_EQ, x, y)
        is_zero = comparor(divisor, lc.Constant.null(divisor.type))
        with cgutils.if_then(builder, is_zero):
            context.raises(ZeroDivisionError, "divide by zero")

def imp_floordiv_signed(context, args):
    a, b = args
    zero_division_check(context, b)
    return context.builder.sdiv(a, b)

def imp_floordiv_unsigned(context, args):
    a, b = args
    zero_division_check(context, b)
    return context.builder.udiv(a, b)

def imp_floordiv_float(intty):
    def imp(context, args):
        a, b = args
        zero_division_check(context, b)
        return context.builder.fptosi(context.builder.fdiv(a, b),
                                      intty.llvm_as_value())
    return imp

# binary truediv

def imp_truediv_float(context, args):
    a, b = args
    zero_division_check(context, b)
    return context.builder.fdiv(a, b)

def imp_truediv_complex(dtype):
    '''
    compute recipocal of a / b = a * (1 / b)
    
    1 / b = 1 / (x + i y) = x / |b| - i y /|b| 
    |b| = x * x + y * y
    '''
    def imp(context, args):
        a, b = args
        x, y = dtype.llvm_unpack(context.builder, b)
        xx = imp_mul_float(context, (x, x))
        yy = imp_mul_float(context, (y, y))
        abs_b = imp_add_float(context, (xx, yy))

        real = imp_truediv_float(context, (x, abs_b))
        imag0 = imp_truediv_float(context, (y, abs_b))
        
        imag = imp_neg_float(dtype.desc.element)(context, (imag0,))

        rb = dtype.desc.llvm_pack(context.builder, real, imag)
        return imp_mul_complex(dtype)(context, (a, rb))
    return imp

# binary mod

def imp_mod_signed(context, args):
    a, b = args
    zero_division_check(context, b)
    return context.builder.srem(a, b)

def imp_mod_unsigned(context, args):
    a, b = args
    zero_division_check(context, b)
    return context.builder.urem(a, b)

def imp_mod_float(context, args):
    a, b = args
    zero_division_check(context, b)
    return context.builder.frem(a, b)

# binary lshift

def out_of_range_shift(context, rhs):
    if context.flags.overflow:
        builder = context.builder
        width = rhs.type.width
        maxval = lc.Constant.int(rhs.type, width)
        outofrange = builder.icmp(lc.ICMP_UGT, rhs, maxval)
        with cgutils.if_then(builder, outofrange):
            context.raises(OverflowError, "shift out-of-range")
        
def imp_lshift_integer(context, args):
    a, b = args
    out_of_range_shift(context, b)
    return context.builder.shl(a, b)

# binary rshift

def imp_rshift_signed(context, args):
    a, b = args
    out_of_range_shift(context, b)
    return context.builder.ashr(a, b)

def imp_rshift_unsigned(context, args):
    a, b = args
    out_of_range_shift(context, b)
    return context.builder.lshr(a, b)

# binary and

def imp_and_integer(context, args):
    a, b = args
    return context.builder.and_(a, b)

# binary or

def imp_or_integer(context, args):
    a, b = args
    return context.builder.or_(a, b)

# binary xor

def imp_xor_integer(context, args):
    a, b = args
    return context.builder.xor(a, b)

# unary negate

def imp_neg_signed(ty):
    def imp(context, args):
        x, = args
        zero = ty.llvm_const(0)
        return imp_sub_signed(context, (zero, x))
    return imp

def imp_neg_float(ty):
    def imp(context, args):
        x, = args
        zero = ty.llvm_const(0)
        return imp_sub_float(context, (zero, x))
    return imp

def imp_neg_complex(ty):
    def imp(context, args):
        x, = args
        zero = ty.llvm_const(0)
        return imp_sub_complex(ty)(context, (zero, x))
    return imp

# unary invert

def imp_invert_integer(ty):
    def imp(context, args):
        x, = args
        ones = lc.Constant.all_ones(ty.llvm_as_value())
        return context.builder.xor(x, ones)
    return imp

# bool eq

def imp_eq_signed(context, args):
    a, b = args
    return context.builder.icmp(lc.ICMP_EQ, a, b)

# bool comparisions

def imp_cmp_signed(cmp, ty):
    CMP = {
        operator.gt: lc.ICMP_SGT,
        operator.lt: lc.ICMP_SLT,
        operator.ge: lc.ICMP_SGE,
        operator.le: lc.ICMP_SLE,
        operator.eq: lc.ICMP_EQ,
        operator.ne: lc.ICMP_NE,
    }
    def imp(context, args):
        a, b = args
        return context.builder.icmp(CMP[cmp], a, b)
    return imp

def imp_cmp_unsigned(cmp, ty):
    CMP = {
        operator.gt: lc.ICMP_UGT,
        operator.lt: lc.ICMP_ULT,
        operator.ge: lc.ICMP_UGE,
        operator.le: lc.ICMP_ULE,
        operator.eq: lc.ICMP_EQ,
        operator.ne: lc.ICMP_NE,
    }
    def imp(context, args):
        a, b = args
        return context.builder.icmp(CMP[cmp], a, b)
    return imp

def imp_cmp_float(cmp, ty):
    CMP = {
        operator.gt: lc.FCMP_OGT,
        operator.lt: lc.FCMP_OLT,
        operator.ge: lc.FCMP_OGE,
        operator.le: lc.FCMP_OLE,
        operator.eq: lc.FCMP_OEQ,
        operator.ne: lc.FCMP_ONE,
    }
    def imp(context, args):
        a, b = args
        return context.builder.fcmp(CMP[cmp], a, b)
    return imp

def imp_cmp_complex(cmp, ty):
    assert cmp in (operator.ne, operator.eq), "no ordering for complex"
    def imp(context, args):
        a, b = args
        areal, aimag = ty.llvm_unpack(context.builder, a)
        breal, bimag = ty.llvm_unpack(context.builder, b)
        cmptor = imp_cmp_float(cmp, ty.desc.element)
        c = cmptor(context, (areal, breal))
        d = cmptor(context, (aimag, bimag))
        return context.builder.and_(c, d)
    return imp

# complex attributes

def imp_complex_real(ty):
    def imp(context, args):
        value, = args
        real, imag = ty.llvm_unpack(context.builder, value)
        return real
    return imp

def imp_complex_imag(ty):
    def imp(context, args):
        value, = args
        real, imag = ty.llvm_unpack(context.builder, value)
        return imag
    return imp

def complex_attributes(complex_type):
    imps = []
    comb = [(imp_complex_real, '.real'), (imp_complex_imag, '.imag')]
    for imp, attrname in comb:
        imps += [Imp(imp(complex_type),
                     attrname,
                     args=(complex_type,),
                     return_type=complex_type.desc.element)]
    return imps

def imp_complex_ctor_1(ty):
    def imp(context, args):
        real, = args
        imag = ty.desc.element.llvm_const(0)
        return ty.desc.llvm_pack(context.builder, real, imag)
    return imp

def imp_complex_ctor_2(ty):
    def imp(context, args):
        real, imag = args
        return ty.desc.llvm_pack(context.builder, real, imag)
    return imp

def complex_ctor(complex_type):
    imp1 = Imp(imp_complex_ctor_1(complex_type),
               complex,
               args=(complex_type.desc.element,),
               return_type=complex_type)
    imp2 = Imp(imp_complex_ctor_2(complex_type),
               complex,
               args=(complex_type.desc.element,) * 2,
               return_type=complex_type)
    return [imp1, imp2]

# casts

def imp_cast_int(fromty):
    def imp(context, args):
        x, = args
        return fromty.llvm_cast(context.builder, x, types.intp)
    return imp

def imp_cast_float(fromty):
    def imp(context, args):
        x, = args
        return fromty.llvm_cast(context.builder, x, types.float64)
    return imp

# fixedarray getitem

def fixedarray_getitem(context, args, argtys, retty):
    ary, ind = args
    aryty, indty = argtys
    assert retty == aryty.desc.element

    bbafter = cgutils.append_block(context.builder)
    bbelse = cgutils.append_block(context.builder)

    switch = context.builder.switch(ind, bbelse, n=aryty.desc.length)

    incomings = []
    for n in range(aryty.desc.length):
        bbcur = cgutils.append_block(context.builder)
        switch.add_case(indty.llvm_const(n), bbcur)
        with cgutils.goto_block(context.builder, bbcur):
            res = context.builder.extract_value(ary, n)
            context.builder.branch(bbafter)
            incomings.append((res, bbcur))

    with cgutils.goto_block(context.builder, bbelse):
        context.builder.unreachable()

    context.builder.position_at_end(bbafter)
    phi = context.builder.phi(retty.llvm_as_value())
    for val, bb in incomings:
        phi.add_incoming(val, bb)

    return phi

# abs

def imp_abs_integer(ty):
    def imp(context, args):
        x, = args
        if not ty.desc.signed:
            raise TypeError("absolute value of %s" % ty)
        zero = ty.llvm_const(0)
        isneg = imp_cmp_signed(operator.lt, ty)(context, (x, zero))
        absval = imp_sub_signed(context, (zero, x))
        return context.builder.select(isneg, absval, x)
    return imp

def imp_abs_float(ty):
    def imp(context, args):
        x, = args
        zero = ty.llvm_const(0)
        isneg = imp_cmp_float(operator.lt, ty)(context, (x, zero))
        absval = imp_sub_float(context, (zero, x))
        return context.builder.select(isneg, absval, x)
    return imp

# min

def imp_min_integer(ty):
    cmpfunc  = imp_cmp_signed if ty.desc.signed else imp_cmp_unsigned
    cmp = cmpfunc(operator.lt, ty)
    def imp(context, args):
        sel = args[0]
        for val in args[1:]:
            pred = cmp(context, (sel, val))
            sel = context.builder.select(pred, sel, val)
        return sel
    return imp

def imp_min_float(ty):
    cmpfunc  = imp_cmp_float
    cmp = cmpfunc(operator.lt, ty)
    def imp(context, args):
        sel = args[0]
        for val in args[1:]:
            pred = cmp(context, (sel, val))
            sel = context.builder.select(pred, sel, val)
        return sel
    return imp

# max

def imp_max_integer(ty):
    cmpfunc  = imp_cmp_signed if ty.desc.signed else imp_cmp_unsigned
    cmp = cmpfunc(operator.gt, ty)
    def imp(context, args):
        sel = args[0]
        for val in args[1:]:
            pred = cmp(context, (sel, val))
            sel = context.builder.select(pred, sel, val)
        return sel
    return imp

def imp_max_float(ty):
    cmpfunc  = imp_cmp_float
    cmp = cmpfunc(operator.gt, ty)
    def imp(context, args):
        sel = args[0]
        for val in args[1:]:
            pred = cmp(context, (sel, val))
            sel = context.builder.select(pred, sel, val)
        return sel
    return imp

#----------------------------------------------------------------------------
# utils

def bool_op_imp(funcobj, imp, typeset):
    return [Imp(imp(funcobj, ty), funcobj,
                args=(ty, ty),
                return_type=types.boolean)
            for ty in typeset]

def binary_op_imp(funcobj, imp, typeset):
    return [Imp(imp, funcobj, args=(ty, ty), return_type=ty)
            for ty in typeset]

def unary_op_imp(funcobj, imp, typeset):
    return [Imp(imp(ty), funcobj, args=(ty,), return_type=ty)
            for ty in typeset]

def floordiv_imp(funcobj, imp, ty, ret):
    return [Imp(imp(ret), funcobj, args=(ty, ty), return_type=ret)]

def casting_imp(funcobj, imp, retty, typeset):
    return [Imp(imp(ty), funcobj, args=(ty,), return_type=retty)
            for ty in typeset]

def minmax_imp(funcobj, imp, typeset, count):
    return [Imp(imp(ty), funcobj, args=(ty,) * count, return_type=ty)
            for ty in typeset]


# --------------------------

builtins = []

# binary add
builtins += binary_op_imp(operator.add, imp_add_signed, typesets.signed_set)
builtins += binary_op_imp(operator.add, imp_add_unsigned, typesets.unsigned_set)
builtins += binary_op_imp(operator.add, imp_add_float, typesets.float_set)
builtins += binary_op_imp(operator.add, imp_add_complex(types.complex64),
                      [types.complex64])
builtins += binary_op_imp(operator.add, imp_add_complex(types.complex128),
                      [types.complex128])

# binary sub
builtins += binary_op_imp(operator.sub, imp_sub_signed, typesets.signed_set)
builtins += binary_op_imp(operator.sub, imp_sub_unsigned, typesets.unsigned_set)
builtins += binary_op_imp(operator.sub, imp_sub_float, typesets.float_set)
builtins += binary_op_imp(operator.sub, imp_sub_complex(types.complex64),
                      [types.complex64])
builtins += binary_op_imp(operator.sub, imp_sub_complex(types.complex128),
                      [types.complex128])

# binary mul
builtins += binary_op_imp(operator.mul, imp_mul_signed, typesets.signed_set)
builtins += binary_op_imp(operator.mul, imp_mul_unsigned, typesets.unsigned_set)
builtins += binary_op_imp(operator.mul, imp_mul_float, typesets.float_set)
builtins += binary_op_imp(operator.mul, imp_mul_complex(types.complex64),
                      [types.complex64])
builtins += binary_op_imp(operator.mul, imp_mul_complex(types.complex128),
                      [types.complex128])

# binary floordiv
builtins += binary_op_imp(operator.floordiv, imp_floordiv_signed,
                      typesets.signed_set)
builtins += binary_op_imp(operator.floordiv, imp_floordiv_unsigned,
                      typesets.unsigned_set)
builtins += floordiv_imp(operator.floordiv, imp_floordiv_float,
                     types.float32, types.int32)
builtins += floordiv_imp(operator.floordiv, imp_floordiv_float,
                     types.float64, types.int64)

# binary truediv
builtins += binary_op_imp(operator.truediv, imp_truediv_float,
                          typesets.float_set)
builtins += binary_op_imp(operator.truediv,
                          imp_truediv_complex(types.complex64),
                          [types.complex64])
builtins += binary_op_imp(operator.truediv,
                          imp_truediv_complex(types.complex128),
                          [types.complex128])

# binary mod
builtins += binary_op_imp(operator.mod, imp_mod_signed, typesets.signed_set)
builtins += binary_op_imp(operator.mod, imp_mod_unsigned, typesets.unsigned_set)
builtins += binary_op_imp(operator.mod, imp_mod_float, typesets.float_set)

# binary lshift
builtins += binary_op_imp(operator.lshift, imp_lshift_integer,
                      typesets.integer_set)
# binary rshift
builtins += binary_op_imp(operator.rshift, imp_rshift_signed,
                      typesets.signed_set)
builtins += binary_op_imp(operator.rshift, imp_rshift_unsigned,
                      typesets.unsigned_set)

# binary and
builtins += binary_op_imp(operator.and_, imp_and_integer,
                      typesets.integer_set)

# binary or
builtins += binary_op_imp(operator.or_, imp_or_integer,
                      typesets.integer_set)

# binary xor
builtins += binary_op_imp(operator.xor, imp_xor_integer,
                      typesets.integer_set)

# bool comparision
for cmp in [operator.gt, operator.ge, operator.lt, operator.le,
            operator.eq, operator.ne]:
    builtins += bool_op_imp(cmp, imp_cmp_signed,   typesets.signed_set)
    builtins += bool_op_imp(cmp, imp_cmp_unsigned, typesets.unsigned_set)
    builtins += bool_op_imp(cmp, imp_cmp_float,    typesets.float_set)

for cmp in [operator.eq, operator.ne]:
    builtins += bool_op_imp(cmp, imp_cmp_complex,  typesets.complex_set)

# unary arith negate
builtins += unary_op_imp(operator.neg, imp_neg_signed, typesets.signed_set)
builtins += unary_op_imp(operator.neg, imp_neg_float, typesets.float_set)
builtins += unary_op_imp(operator.neg, imp_neg_complex, typesets.complex_set)

# unary logical negate
builtins += unary_op_imp(operator.invert, imp_invert_integer,
                     typesets.integer_set)

# complex attributes
for complex_type in typesets.complex_set:
    builtins += complex_attributes(complex_type)
    builtins += complex_ctor(complex_type)

# casts
builtins += casting_imp(int, imp_cast_int, types.intp,
               typesets.integer_set|typesets.float_set|typesets.complex_set)
builtins += casting_imp(float, imp_cast_float, types.float64,
               typesets.integer_set|typesets.float_set|typesets.complex_set)

# fixedarray getitem
builtins += [Imp(fixedarray_getitem, operator.getitem,
             args=(types.FixedArrayKind, types.intp))]

# abs
builtins += unary_op_imp(abs, imp_abs_integer, typesets.integer_set)
builtins += unary_op_imp(abs, imp_abs_float, typesets.float_set)

# min
builtins += minmax_imp(min, imp_min_integer, typesets.integer_set, 2)
builtins += minmax_imp(min, imp_min_integer, typesets.integer_set, 3)
builtins += minmax_imp(min, imp_min_float, typesets.float_set, 2)
builtins += minmax_imp(min, imp_min_float, typesets.float_set, 3)

# max
builtins += minmax_imp(max, imp_max_integer, typesets.integer_set, 2)
builtins += minmax_imp(max, imp_max_integer, typesets.integer_set, 3)
builtins += minmax_imp(max, imp_max_float, typesets.float_set, 2)
builtins += minmax_imp(max, imp_max_float, typesets.float_set, 3)

# --------------------------

def populate_builtin_impl(implib):
    for imp in builtins:
        implib.define(imp)
