import torch
from operator import itemgetter
import torch.nn.functional as F
from torch import nn
import pdb

# ---------------------Test axial attention module----------------------
def map_el_ind(arr, ind):
    return list(map(itemgetter(ind), arr))
def sort_and_return_indices(arr):
    indices = [ind for ind in range(len(arr))]
    arr = zip(arr, indices)
    arr = sorted(arr)
    return map_el_ind(arr, 0), map_el_ind(arr, 1)
def calculate_permutations(num_dimensions, emb_dim):
    total_dimensions = num_dimensions + 2
    emb_dim = emb_dim if emb_dim > 0 else (emb_dim + total_dimensions)
    axial_dims = [ind for ind in range(1, total_dimensions) if ind != emb_dim]

    permutations = []

    for axial_dim in axial_dims:
        last_two_dims = [axial_dim, emb_dim]
        dims_rest = set(range(0, total_dimensions)) - set(last_two_dims)
        permutation = [*dims_rest, *last_two_dims]
        permutations.append(permutation)

    return permutations
class PermuteToFrom(nn.Module):
    def __init__(self, permutation, fn):
        super().__init__()
        self.fn = fn
        _, inv_permutation = sort_and_return_indices(permutation)
        self.permutation = permutation
        self.inv_permutation = inv_permutation

    def forward(self, x, **kwargs):
        # x.shape = torch.Size([4, 256, 128, 128])
        axial = x.permute(*self.permutation).contiguous() # axial.shape = torch.Size([4, 128, 128, 256])
        shape = axial.shape
        *_, t, d = shape

        # merge all but axial dimension
        axial = axial.reshape(-1, t, d) # axial.shape = torch.Size([512, 128, 256])

        # attention
        axial = self.fn(axial, **kwargs) # torch.Size([512, 128, 256])

        # restore to original shape and permutation
        axial = axial.reshape(*shape) # torch.Size([4, 128, 128, 256])
        axial = axial.permute(*self.inv_permutation).contiguous() # axial.shape = torch.Size([4, 256, 128, 128])
        return axial

class SelfAttention(nn.Module):
    def __init__(self, dim, heads, dim_heads = None):
        super().__init__()
        self.dim_heads = (dim // heads) if dim_heads is None else dim_heads
        dim_hidden = self.dim_heads * heads

        self.heads = heads
        self.to_q = nn.Linear(dim, dim_hidden, bias = False)
        self.to_kv = nn.Linear(dim, 2 * dim_hidden, bias = False)
        self.to_out = nn.Linear(dim_hidden, dim)

    def forward(self, x, kv = None):
        kv = x if kv is None else kv
        q, k, v = (self.to_q(x), *self.to_kv(kv).chunk(2, dim=-1))
        # batch, dim,
        b, t, d, h, e = *q.shape, self.heads, self.dim_heads

        # merge axial and batch
        merge_heads = lambda x: x.reshape(b, -1, h, e).transpose(1, 2).reshape(b * h, -1, e)
        q, k, v = map(merge_heads, (q, k, v))

        dots = torch.einsum('bie,bje->bij', q, k) * (e ** -0.5)
        dots = dots.softmax(dim=-1)
        out = torch.einsum('bij,bje->bie', dots, v)

        out = out.reshape(b, h, -1, e).transpose(1, 2).reshape(b, -1, d)
        out = self.to_out(out)
        return out

class AxialAttention(nn.Module):
    def __init__(self, dim, num_dimensions = 2, heads = 8, dim_heads = None, dim_index = -1, sum_axial_out = True):
        assert (dim % heads) == 0, 'hidden dimension must be divisible by number of heads'
        super().__init__()
        self.dim = dim
        self.total_dimensions = num_dimensions + 2
        self.dim_index = dim_index if dim_index > 0 else (dim_index + self.total_dimensions)

        attentions = []
        for permutation in calculate_permutations(num_dimensions, dim_index):
            attentions.append(PermuteToFrom(permutation, SelfAttention(dim, heads, dim_heads)))

        self.axial_attentions = nn.ModuleList(attentions)
        self.sum_axial_out = sum_axial_out

    def forward(self, x):
        assert len(x.shape) == self.total_dimensions, 'input tensor does not have the correct number of dimensions'
        assert x.shape[self.dim_index] == self.dim, 'input tensor does not have the correct input dimension'

        if self.sum_axial_out:
            return sum(map(lambda axial_attn: axial_attn(x), self.axial_attentions))

        out = x
        for axial_attn in self.axial_attentions:
            out = axial_attn(out)
        return out
# --------------------testing----------------------
# define testing input array
if __name__ == '__main__':
    pred = torch.randn(4, 256, 128, 128)
    mask = torch.randint(0, 2, (4, 1, 128, 128)).float()
    # _, inv_permutation = sort_and_return_indices([0,3,2,1]) # return [0, 1, 2, 3] [0, 3, 2, 1]
    # print(_, inv_permutation)
    # x = PermuteToFrom([0,3,2,1],SelfAttention(dim=256, heads=8))
    # print(x(pred).shape) # input torch.Size([4, 1, 128, 128]), return torch.Size([4, 1, 128, 128])
    attn = AxialAttention(dim=256, num_dimensions=2, heads=8, dim_index=-3) # dim_index is dim index
    result = attn(pred)
    print(pred.shape)