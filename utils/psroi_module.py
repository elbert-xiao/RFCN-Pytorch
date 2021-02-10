import cupy, torch
import cupy as cp
import torch as t
from torch._six import container_abcs
from itertools import repeat

from string import Template
from torch.autograd import Function
from collections import namedtuple
from utils.psroi_cuda import kernel_forward_t, kernel_backward_t

Stream = namedtuple('Stream', ['ptr'])
CUDA_NUM_THREADS = 1024  # threads of each block

def t_ntuple(n):
    def parse(x):
        if isinstance(x, container_abcs.Iterable):
            return x
        return tuple(repeat(x, n))
    return parse


t_pair = t_ntuple(2)


@cp.memoize(True)
def load_kernel(kernel_name, code, **kwargs):
    cp.cuda.runtime.free(0)

    # replace code with input params
    code = Template(code).substitute(**kwargs)
    kernel_code = cupy.cuda.compile_with_cache(code)
    return kernel_code.get_function(kernel_name)


def GET_BLOCKS(N, K=CUDA_NUM_THREADS):
    return (N + K - 1) // K


psROI_backward_fn = load_kernel('PSROIPoolBackward', kernel_backward_t)

class psRoI_Info:
    def __init__(self):
        self.forward_fn = load_kernel('PSROIPoolForward', kernel_forward_t)

        self.outh, self.outw, self.spatial_scale = None, None, None
        self.group_size = None

    def set_para(self, pool_size, spatial_scale, group_size=None):
        self.outh, self.outw, self.spatial_scale = pool_size[0], pool_size[1], spatial_scale
        if group_size is None:
            if pool_size[0] != pool_size[1]:
                raise ValueError("pool_h_size must be equal with pool_w_size when the group_size is None")
            self.group_size = pool_size[0]
        else:
            self.group_size = group_size


class psRoI(Function):
    @staticmethod
    def forward(ctx, x, rois, Info: psRoI_Info):
        """
        :param ctx:     context variable(similar to 'self')
        :param x:       input feature map
        :param rois:    rois generated by rpn,
                        note:this 'rois' is indices_and_rois combined indexes and rois
                        ==> [batch_ind, x_min, y_min, x_max, y_max]
        :return:
        """
        # Ensure memory contiguous
        x = x.contiguous()
        rois = rois.contiguous()

        in_size = B, C, H, W = x.size()  # e.g.(b, 21 * 7 * 7, h, w)
        N = rois.size(0)  # the numbers of roi
        if C % (Info.group_size * Info.group_size) != 0:
            raise ValueError("The group_size must be an integral multiple of input_channel!")
        out_dim = C // (Info.group_size * Info.group_size)

        output = t.zeros(N, out_dim, Info.outh, Info.outw).cuda()  # Used to save output
        count = output.numel()  # the number of sub regions for psROI
        mapping_channel = torch.zeros(count, dtype=cp.int).cuda()  # hich channel is the bottom data in

        # Packing parameters
        args = [count,
                x.data_ptr(),
                cp.float32(Info.spatial_scale),  # must convert float param to cp.float32
                C, H, W,
                Info.outh,
                Info.outw,
                rois.data_ptr(),
                out_dim,
                Info.group_size,
                output.data_ptr(),
                mapping_channel.data_ptr(),
        ]

        # create cuda stream so that Kernel calculation and data transmission can be executed asynchronously
        stream = Stream(ptr=torch.cuda.current_stream().cuda_stream)

        # using one-dimensional index for block and thread
        Info.forward_fn(args=args,
                        block=(CUDA_NUM_THREADS, 1, 1),
                        grid=(GET_BLOCKS(count), 1, 1),
                        stream=stream)

        # save info for backward
        saveBackwardInfo_int = [count, N, out_dim, Info.outh, Info.outw]
        saveBackwardInfo_int = torch.tensor(saveBackwardInfo_int)

        ctx.save_for_backward(saveBackwardInfo_int, torch.tensor(in_size),
                              torch.tensor(Info.spatial_scale), rois, mapping_channel)

        return output

    @staticmethod
    def backward(ctx, grad_output):
        """
        the backward of psRoI_pooling
        :param ctx:         context variable
        :param grad_output: gradient input(backward) of psRoI module
        :return:
        """
        # Here we must handle None grad_output tensor. In this case we
        # can skip unnecessary computations and just return None.
        if grad_output is None:
            return None, None, None

        grad_output = grad_output.contiguous()

        int_info, in_size, spatial_scale, rois, mapping_channel = ctx.saved_tensors
        count, N, out_dim, outh, outw = int_info.tolist()
        in_size = tuple(in_size.tolist())
        B, C, H, W = in_size                   # e.g.(b, 21 * 7 * 7, h, w)
        grad_input = t.zeros(in_size).cuda()   # developing cuda memory to save gradient for output

        # create cuda stream
        stream = Stream(ptr=torch.cuda.current_stream().cuda_stream)

        args = [count,
                grad_output.data_ptr(),
                mapping_channel.data_ptr(),
                N,
                cp.float32(spatial_scale),
                C, H, W,
                outh, outw,
                out_dim,
                grad_input.data_ptr(),
                rois.data_ptr(),
                ]

        psROI_backward_fn(args=args,
                          block=(CUDA_NUM_THREADS, 1, 1),
                          grid=(GET_BLOCKS(grad_output.numel()), 1, 1),
                          stream=stream)

        return grad_input, None, None  # The 'None' indicates that backpropagation to RPN and info is ignored


class PSRoIPooling2D(t.nn.Module):
    def __init__(self, pool_size, spatial_scale, group_size=None):
        super(PSRoIPooling2D, self).__init__()
        pool_size = t_pair(pool_size)

        # i.e. pool_size, spatial_scale = (7, 7), 1./16
        self.RoI_Info = psRoI_Info()
        self.RoI_Info.set_para(pool_size, spatial_scale, group_size=group_size)

        self.psROI_md = psRoI()

    def forward(self, x, rois):
        """
        PS_ROI pooling forward
        :param x:       input feature map
        :param rois:    rois generated by rpn,
                        note:this 'rois' is indices_and_rois combined indexes and rois
                        ==> [batch_ind, x_min, y_min, x_max, y_max]
        :return:        output
        """
        return self.psROI_md.apply(x, rois, self.RoI_Info)


def acitvate_PsROI_for_eval(model: PSRoIPooling2D):
    """
    backward once first to speed up eval(cause of an hidden conflict with SkImage lib?)
    :return:
    """
    # fake data
    class_num = 21
    group_size = 7
    B, C, H, W, PH, PW = 2, class_num*group_size*group_size, 28, 28, 21, 21
    bottom_data = t.randn((B, C, H, W)).cuda()

    # rois
    rois = [torch.tensor([[0, 0, 112, 112], [7, 75, 503, 442]], dtype=torch.float),
            torch.tensor([[0, 0, 224, 224]], dtype=torch.float)]
    indices = torch.tensor([0, 0, 1])
    rois2 = torch.cat(rois, dim=0)
    indices = torch.reshape(indices, (-1, 1))
    rois2_with_indices = torch.hstack((indices, rois2))
    bottom_rois = rois2_with_indices.cuda()

    x = bottom_data.detach().requires_grad_()
    rois = bottom_rois.detach()

    output = model(x, rois)
    output.sum().backward()

