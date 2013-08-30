'''Example: sum each row using guvectorize

See Numpy documentation for detail about gufunc:
    http://docs.scipy.org/doc/numpy/reference/c-api.generalized-ufuncs.html
'''
import math
import numpy as np
from numbapro import guvectorize, cuda
from .support import testcase, main

@testcase
def test_gufunc_scalar_output():
#    function type:
#        - has no void return type
#        - array argument is one dimenion fewer than the source array
#        - scalar output is passed as a 1-element array.
#
#    signature: (n)->()
#        - the function takes an array of n-element and output a scalar.

    @guvectorize(['void(int32[:], int32[:])'], '(n)->()', target='gpu')
    def sum_row(inp, out):
        tmp = 0.
        for i in range(inp.shape[0]):
            tmp += inp[i]
        out[0] = tmp

    # inp is (10000, 3)
    # out is (10000)
    # The outter (leftmost) dimension must match or numpy broadcasting is performed.
    # But, broadcasting on CUDA arrays is not supported.

    inp = np.arange(300, dtype=np.int32).reshape(100, 3)

    # invoke on CUDA with manually managed memory
    out1 = np.empty(100, dtype=inp.dtype)
    out2 = np.empty(100, dtype=inp.dtype)

    dev_inp = cuda.to_device(inp)                 # alloc and copy input data
    dev_out1 = cuda.to_device(out1, copy=False)   # alloc only

    sum_row(dev_inp, out=dev_out1)                # invoke the gufunc
    dev_out2 = sum_row(dev_inp)                   # invoke the gufunc

    dev_out1.copy_to_host(out1)                 # retrieve the result
    dev_out2.copy_to_host(out2)                 # retrieve the result

    # verify result
    for i in xrange(inp.shape[0]):
        assert out1[i] == inp[i].sum()
        assert out2[i] == inp[i].sum()

@testcase
def test_gufunc_scalar_input_saxpy():
    @guvectorize(['void(float32, float32[:], float32[:], float32[:])'],
                 '(),(t),(t)->(t)', target='gpu')
    def saxpy(a, x, y, out):
        for i in range(out.shape[0]):
            out[i] = a * x[i] + y[i]

    A = np.float32(2)
    X = np.arange(10, dtype=np.float32).reshape(5,2)
    Y = np.arange(10, dtype=np.float32).reshape(5,2)
    out = saxpy(A, X, Y)

    for j in range(5):
        for i in range(2):
            exp = A * X[j, i] + Y[j, i]
            assert exp == out[j, i]

    A = np.arange(5, dtype=np.float32)
    out = saxpy(A, X, Y)

    for j in range(5):
        for i in range(2):
            exp = A[j] * X[j, i] + Y[j, i]
            assert exp == out[j, i]


#@testcase
#def test_gufunc_scalar_input_2():
#    @guvectorize(['void(f8[:], f8[:], f8[:])'], '(t),(scalar)->(t)',
#                 target='gpu')
#    def exp_avg(arr_t, decay_length, out_t):
#        decay_factor = math.exp(-1.0 / decay_length[0])
#        sum_w = 0.0
#        sum_wx = 0.0
#       
#        n_t = arr_t.shape[0]
#        for t in xrange(n_t):
#            sum_w += 1.0
#            sum_wx += arr_t[t]
#            out_t[t] = sum_wx / sum_w
#           
#            sum_w *= decay_factor
#            sum_wx *= decay_factor
#
#    np.arange(10, dtype=np.float64)
#    exp_avg()

if __name__ == '__main__':
    main()
