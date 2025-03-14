#perhaps add multiway and rel_pos, conditional mutlihead multimodality attention
#omni modality attention meta function/mechansm
import math
from functools import partial
from colt5_attention.xpos import XPOS


import torch
import torch.nn.functional as F
from torch import nn, einsum

from local_attention import LocalMHA
from einops import rearrange, repeat, pack, unpack

from colt5_attention.attend import Attend

from colt5_attention.positionsBiases import LearnedAlibiPositionalBias
from colt5_attention.multiway import MultiwayWrapper
from colt5_attention.relative_position_bias import RelativePositionBias



# helper functions

def exists(val):
    return val is not None

def default(val, d):
    return val if exists(val) else d

def divisible_by(numer, denom):
    return (numer % denom) == 0

def pack_one(t, pattern):
    return pack([t], pattern)

def unpack_one(t, ps, pattern):
    return unpack(t, ps, pattern)[0]

def pad_to_multiple(tensor, multiple, dim=-1, value=0):
    seq_len = tensor.shape[dim]
    m = seq_len / multiple
    if m.is_integer():
        return tensor, seq_len

    remainder = math.ceil(m) * multiple - seq_len
    pad_offset = (0,) * (-1 - dim) * 2
    padded_tensor = F.pad(tensor, (*pad_offset, 0, remainder), value = value)
    return padded_tensor, seq_len

def batched_gather(x, indices):
    batch_range = create_batch_range(indices, indices.ndim - 1)
    return x[batch_range, indices]

# tensor helpers

def create_batch_range(t, right_pad_dims = 1):
    b, device = t.shape[0], t.device
    batch_range = torch.arange(b, device = device)
    pad_dims = ((1,) * right_pad_dims)
    return batch_range.reshape(-1, *pad_dims)

# normalization

class RMSNorm(nn.Module):
    def __init__(self, dim):
        super().__init__()
        self.scale = dim ** 0.5
        self.gamma = nn.Parameter(torch.ones(dim))

    def forward(self, x):
        normed = F.normalize(x, dim = -1)
        return normed * self.scale * self.gamma

# modules

def FeedForward(dim, mult = 4):
    dim_hidden = int(dim * mult)
    return nn.Sequential(
        RMSNorm(dim),
        nn.Linear(dim, dim_hidden),
        nn.GELU(),
        nn.Linear(dim_hidden, dim)
    )

class Attention(nn.Module):
    def __init__(
        self,
        dim,
        dim_head = 64,
        heads = 8,
        multiply_keys_by_score = False,
        use_flash = False
    ):
        super().__init__()
        self.heads = heads
        self.scale = dim_head ** -0.5
        dim_hidden = dim_head * heads

        self.multiply_keys_by_score = multiply_keys_by_score

        self.norm = RMSNorm(dim)
        self.null_kv = nn.Parameter(torch.randn(2, heads, dim_head))

        self.attend = Attend(use_flash = use_flash)

        self.to_q = nn.Linear(dim, dim_hidden, bias = False)
        self.to_kv = nn.Linear(dim, dim_hidden * 2, bias = False)
        self.to_out = nn.Linear(dim_hidden, dim, bias = False)

    def forward(
        self,
        x,
        context = None,
        mask = None,
        normalized_scores_kv = None,
        normalized_scores_q = None
    ):
        """
        einops:
        b - batch
        h - heads, or number of heads per route
        r - routing dimension, for routing different sets of key / values - should be more expressive
        n - sequence dimension
        d - head dimension
        i - input model dimension
        """

        batch, h = x.shape[0], self.heads

        x = self.norm(x)

        if exists(context):
            context = self.norm(context)

        context = default(context, x)

        # if routing dimension is not there, unsqueeze for 1 routing dimension

        if context.ndim == 3:
            context = rearrange(context, 'b n d -> b 1 n d')

        if exists(normalized_scores_kv) and isinstance(normalized_scores_kv, torch.Tensor):
            if normalized_scores_kv.ndim == 2:
                normalized_scores_kv = rearrange(normalized_scores_kv, 'b n -> b 1 n')

            normalized_scores_kv = rearrange(normalized_scores_kv, 'b r n -> b r 1 n 1')

        num_kv_routes = context.shape[1]

        # get queries

        q = self.to_q(x)
        q = rearrange(q, 'b n (h d) -> b h n d', h = h)

        if exists(normalized_scores_q) and isinstance(normalized_scores_q, torch.Tensor):
            q = q * rearrange(normalized_scores_q, 'b n -> b 1 n 1')

        # handle key / values, with the routing dimension, dividing the number of heads in between the routes

        assert divisible_by(h, num_kv_routes), 'number of heads must be divisible by the number of key / value routes'
        heads_per_route = h // num_kv_routes

        kv_weight = rearrange(self.to_kv.weight, '(r h d) i -> r h d i', h = heads_per_route, r = num_kv_routes)

        kv = einsum('r h d i, b r n i -> b r h n d', kv_weight, context)
        k, v = kv.chunk(2, dim = -1)

        if exists(normalized_scores_kv):
            # in paper, not sure how they passed back the signal from heavy attention to normalized scores for key/values. just multiply the values by the normalized kv scores for now
            v = v * normalized_scores_kv

            if self.multiply_keys_by_score:
                k = k * normalized_scores_kv

        k, v = map(lambda t: rearrange(t, 'b r h n d -> b (r h) n d'), (k, v))

        # null key values

        nk, nv = map(lambda t: repeat(t, 'h d -> b h 1 d', b = batch), self.null_kv)

        k = torch.cat((nk, k), dim = -2)
        v = torch.cat((nv, v), dim = -2)

        # masking

        if exists(mask):
            if mask.ndim == 3:
                mask = repeat(mask, 'b r j -> b (r h) 1 j', h = heads_per_route)
            else:
                mask = rearrange(mask, 'b j -> b 1 1 j')

            mask = F.pad(mask, (1, 0), value = True)

        # attention

        out = self.attend(q, k, v)

        # merge heads

        out = rearrange(out, 'b h n d -> b n (h d)')
        return self.to_out(out)

# routing related logic

from colt5_attention.coor_descent import coor_descent

class CoordinateDescentRouter(nn.Module):
    """
    from Wright et al. https://arxiv.org/abs/1502.04759
    then adopted by https://arxiv.org/abs/2211.01267 for multi-vector document retrieval by Qian et al
    finally, used successfully by this paper for routing to heavy branch attention / feedforward
    """

    def __init__(
        self,
        dim,
        straight_through = True,
        n_iters = 50,                   # 50 iterations in the paper
        fetch_k_ratio = 9 / 8,          # in the paper, they do a bit slightly higher k (times this ratio) for better learning
        eps = 1.,                       # the epsilon for coordinate descent. in CoLT5 paper they used 1. apparently
        num_routing_tokens = 1,
        use_triton = False,
        route_block_size = None,
        triton_checkpoint_segments = 4  # whether to recompute the coordinate descent in segments, with 4 and 50 iterations, backwards is sped up 3x times at the expense of forwards and some memory for saving initial a and b
    ):
        super().__init__()
        assert fetch_k_ratio >= 1.
        self.eps = eps

        self.n_iters = n_iters
        self.fetch_k_ratio = fetch_k_ratio

        self.coor_descent = coor_descent

        if use_triton:
            from colt5_attention.triton_coor_descent import triton_coor_descent
            self.coor_descent = partial(triton_coor_descent, checkpoint_segments = triton_checkpoint_segments)

        self.is_one_routing_token = num_routing_tokens == 1
        self.num_routing_tokens = num_routing_tokens

        self.route_block_size = route_block_size

        self.routing_token = nn.Parameter(torch.randn(num_routing_tokens, dim))
        self.straight_through = straight_through

    def route_back(self, src, routed_tokens, indices):
        batch_range = create_batch_range(routed_tokens)
        src[batch_range, indices] = routed_tokens
        return src

    def forward(
        self,
        x,
        *,
        num_tokens,
        mask = None
    ):
        n, device, eps, num_routes, route_block_size = x.shape[-2], x.device, self.eps, self.num_routing_tokens, self.route_block_size

        # whether to route even amounts from blocks of the sequence

        if exists(route_block_size):
            num_blocks = n // route_block_size
            prev_seq_mult = num_blocks * route_block_size

            # just curtail to last multiple of route block size

            x = x[:, :prev_seq_mult]

            # group sequence into blocks to route

            x = rearrange(x, 'b (n w) d -> (b n) w d', w = route_block_size)

            if exists(mask):
                mask = mask[:, :prev_seq_mult]
                mask = rearrange(mask, 'b (n w) -> (b n) w', w = route_block_size)

            n = route_block_size
            num_tokens = math.ceil(num_tokens / num_blocks)

        # s stands for eventual normalized score

        s = einsum('b n d, r d -> b r n', x, self.routing_token)

        # merge routing dimension into batch

        x = repeat(x, 'b ... -> (b r) ...', r = num_routes)
        s, ps = pack_one(s, '* n')

        if exists(mask):
            mask = repeat(mask, 'b ... -> (b r) ...', r = num_routes)

        # k, which controls the sparsity of the outputted scores from iterative coordinate descent

        effective_k = min(num_tokens * self.fetch_k_ratio, n)

        k = torch.tensor([effective_k], device = device)

        # coordinate descent

        scores = self.coor_descent(
            s,
            n_iters = self.n_iters,
            mask = mask,
            k = k,
            eps = eps
        )

        # get the topk scores and indices from the sparse matrix

        selected_scores, selected_indices = scores.topk(num_tokens, dim = -1)

        if self.straight_through:
            # this would make sure all normalized scores returned are 1., but still differentiable using straight-through trick
            selected_scores = selected_scores + (1. - selected_scores).detach()

            if exists(mask):                
                selected_mask = batched_gather(mask, selected_indices)
                selected_scores = selected_scores.masked_fill(~selected_mask, 0.)

        # split out routing dimension again if need be

        if not self.is_one_routing_token:
            selected_scores = unpack_one(selected_scores, ps, '* n')
            selected_indices = unpack_one(selected_indices, ps, '* n')

        # undo the windowing, if one were routing uniformly in blocks

        if exists(route_block_size):
            selected_scores = rearrange(selected_scores, '(b n) ... w -> b ... (n w)', n = num_blocks)
            selected_indices = rearrange(selected_indices, '(b n) ... w -> b ... n w', n = num_blocks)

            indices_offset = torch.arange(num_blocks, device = device) * route_block_size
            selected_indices = selected_indices + rearrange(indices_offset, 'n -> n 1')
            selected_indices = rearrange(selected_indices, 'b ... n w -> b ... (n w)')

        return selected_scores, selected_indices

# main classes

class ConditionalRoutedFeedForward(nn.Module):
    def __init__(
        self,
        dim,
        *,
        num_heavy_tokens,
        light_ff_mult = 0.5,
        heavy_ff_mult = 4,
        router_straight_through = True, # would make sure all normalized scores are 1., still differentiable
        router_kwargs: dict = {},
        use_triton = False
    ):
        super().__init__()
        self.num_heavy_tokens = num_heavy_tokens

        if use_triton:
            router_kwargs = {**router_kwargs, 'use_triton': True}

        self.router = CoordinateDescentRouter(
            dim = dim,
            straight_through = router_straight_through,
            **router_kwargs
        )

        self.light_ff = FeedForward(dim, light_ff_mult)
        self.heavy_ff = FeedForward(dim, heavy_ff_mult)


    def forward(
        self,
        x,
        mask = None,
        num_heavy_tokens = None
    ):
        device, num_heavy_tokens = x.device, default(num_heavy_tokens, self.num_heavy_tokens)

        # light feedforward sees all the tokens (hidden dimension is only 1/2 of model dimensions)

        light_out = self.light_ff(x)

        # route tokens appropriately for heavy branch

        normalized_scores, indices = self.router(x, num_tokens = num_heavy_tokens, mask = mask)

        # select the tokens to be routed to heavier feedforward (hidden dimension is 4 times model dimensions)

        routed_tokens = batched_gather(x, indices)

        # do the heavier branch with only routed tokens

        routed_tokens_out = self.heavy_ff(routed_tokens) * rearrange(normalized_scores, '... -> ... 1')

        # scatter back the output of the heavy feedforward branch

        heavy_out = torch.zeros_like(x)

        heavy_out = self.router.route_back(heavy_out, routed_tokens_out, indices)

        # sum light and heavy branches

        return light_out + heavy_out


# #v2
# class ConditionalRoutedAttention(nn.Module):
#     def __init__(
#         self,
#         dim,
#         *,
#         num_heavy_tokens_q,
#         num_heavy_tokens_kv,
#         num_routed_kv = 1,
#         light_dim_head = 64,
#         light_heads = 8,
#         light_window_size = 128,        # each token would see ~ 64 tokens either way to left or right
#         heavy_dim_head = 64,
#         heavy_heads = 8,
#         router_straight_through = True, # would make sure all normalized scores are 1., still differentiable
#         router_kwargs: dict = {},
#         multiply_keys_by_score = False,
#         multiply_queries_by_score = False,
#         use_triton = False,
#         use_null_q_tokens = True,
#         use_flash_attn = False,
#         use_alibi = False,
#         use_xpos = False,
#         xpos_head_dim = None,
#         xpos_scale_base = 512,
#         multiway=False,
#         use_relative_position_bias=False,
#     ):
#         super().__init__()

#         if use_triton:
#             router_kwargs = {**router_kwargs, 'use_triton': True}

        
#         #alibi
#         self.use_alibi = use_alibi

#         if use_alibi:
#             if not alibi_heads:
#                 alibi_heads = light_heads
#             self.alibi_bias = LearnedAlibiPositionalBias(alibi_heads)
#         #alibi

#         self.use_xpos = use_xpos
#         if use_xpos:
#             if not xpos_head_dim:
#                 xpos_head_dim = dim // light_heads
#             self.xpos = XPOS(xpos_head_dim, scale_base=xpos_scale_base)

#         #relative position bias
#         self.use_relative_position_bias = use_relative_position_bias
#         if use_relative_position_bias:
#             self.relative_position_bias = RelativePositionBias()
#         #================>



#         self.num_heavy_tokens_q = num_heavy_tokens_q
#         self.num_heavy_tokens_kv = num_heavy_tokens_kv

#         self.multiply_queries_by_score = multiply_queries_by_score

#         self.light_attn = MultiwayWrapper(
#             {"multiway": multiway},
#             LocalMHA(
#                 dim=dim,
#                 dim_head=light_dim_head,
#                 heads=light_heads,
#                 window_size=light_window_size // 2,
#                 prenorm=True,
#                 causal=False,
#                 use_rotary_pos_emb=True,
#                 look_backward=1,
#                 look_forward=1,
#             ),
#         )


#         self.null_q_token = None
#         if use_null_q_tokens:
#             self.null_q_token = nn.Parameter(torch.randn(dim)) # for the query tokens not selected by the router, give it a learned output embed

#         self.q_router = CoordinateDescentRouter(
#             dim = dim,
#             straight_through = router_straight_through,
#             **router_kwargs
#         )

#         self.kv_router = CoordinateDescentRouter(
#             dim = dim,
#             num_routing_tokens = num_routed_kv,
#             straight_through = router_straight_through,
#             **router_kwargs
#         )

#         self.heavy_attn = Attention(
#             dim = dim,
#             dim_head = heavy_dim_head,
#             heads = heavy_heads,
#             multiply_keys_by_score = multiply_keys_by_score,
#             use_flash = use_flash_attn
#         )


#     def forward(
#         self,
#         x,
#         *,
#         num_heavy_tokens_q = None,
#         num_heavy_tokens_kv = None,
#         mask = None
#     ):
#         batch, device = x.shape[0], x.device
#         qlen, klen = x.shape[1], x.shape[1]

#         num_heavy_tokens_q = default(num_heavy_tokens_q, self.num_heavy_tokens_q)
#         num_heavy_tokens_kv = default(num_heavy_tokens_kv, self.num_heavy_tokens_kv)


#         # light local attention sees all tokens in a limited context
#         # light_out = self.light_attn(x, mask = mask)
#         if self.use_relative_position_bias:
#             light_out = self.light_attn(x, mask=mask)
#             light_out += self.relative_position_bias(batch, qlen, klen)
#         else:
#             light_out = self.light_attn(x, mask=mask)
            
        
        
#         #use alibi

#         if self.use_alibi:
#             qk_dots = light_out
#             light_out = self.alibi_bias(qk_dots)
#         #=-===============alibi end

#         if self.use_xpos:
#             light_out = self.xpos(light_out)

#         # route tokens appropriately for heavy branch

#         normalized_scores_q, indices_q = self.q_router(x, num_tokens = num_heavy_tokens_q, mask = mask)
#         normalized_scores_kv, indices_kv = self.kv_router(x, num_tokens = num_heavy_tokens_kv, mask = mask)

#         # select the tokens to be routed to full attention

#         routed_tokens_q = batched_gather(x, indices_q)

#         kv_batch_range = create_batch_range(x, right_pad_dims = indices_kv.ndim - 1)
#         routed_tokens_kv = batched_gather(x, indices_kv)

#         # calculate key padding mask

#         routed_tokens_kv_mask = None
#         if exists(mask):
#             routed_tokens_kv_mask = mask[kv_batch_range, indices_kv]

#         # do the heavier branch with only routed tokens

#         # routed_tokens_out = self.heavy_attn(
#         #     routed_tokens_q,
#         #     mask = routed_tokens_kv_mask,
#         #     context = routed_tokens_kv,
#         #     normalized_scores_kv = normalized_scores_kv,
#         #     normalized_scores_q = normalized_scores_q if self.multiply_queries_by_score else None
#         # )

#         # do the heavier branch with only routed tokens
#         if self.use_relative_position_bias:
#             routed_tokens_out = self.heavy_attn(
#                 routed_tokens_q,
#                 mask=routed_tokens_kv_mask,
#                 context=routed_tokens_kv,
#                 normalized_scores_kv=normalized_scores_kv,
#                 normalized_scores_q=normalized_scores_q if self.multiply_queries_by_score else None
#             )
#             routed_tokens_out += self.relative_position_bias(batch, qlen, klen).squeeze(0)
#         else:
#             routed_tokens_out = self.heavy_attn(
#                 routed_tokens_q,
#                 mask=routed_tokens_kv_mask,
#                 context=routed_tokens_kv,
#                 normalized_scores_kv=normalized_scores_kv,
#                 normalized_scores_q=normalized_scores_q if self.multiply_queries_by_score else None
#             )


#         routed_tokens_out = routed_tokens_out * rearrange(normalized_scores_q, '... -> ... 1')

#         # scatter back the output of the heavy branch

#         if exists(self.null_q_token):
#             heavy_out = rearrange(self.null_q_token, 'd -> 1 1 d')
#             heavy_out = heavy_out.expand_as(x).clone()
#         else:
#             heavy_out = torch.zeros_like(x)

#         heavy_out = self.q_router.route_back(heavy_out, routed_tokens_out, indices_q)

#         # sum light and heavy branches

#         return light_out + heavy_out



#v1 normal:

class ConditionalRoutedAttention(nn.Module):
    def __init__(
        self,
        dim,
        *,
        num_heavy_tokens_q,
        num_heavy_tokens_kv,
        num_routed_kv = 1,
        light_dim_head = 64,
        light_heads = 8,
        light_window_size = 128,        # each token would see ~ 64 tokens either way to left or right
        heavy_dim_head = 64,
        heavy_heads = 8,
        router_straight_through = True, # would make sure all normalized scores are 1., still differentiable
        router_kwargs: dict = {},
        multiply_keys_by_score = False,
        multiply_queries_by_score = False,
        use_triton = False,
        use_null_q_tokens = True,
        use_flash_attn = False
    ):
        super().__init__()

        if use_triton:
            router_kwargs = {**router_kwargs, 'use_triton': True}

        self.num_heavy_tokens_q = num_heavy_tokens_q
        self.num_heavy_tokens_kv = num_heavy_tokens_kv

        self.multiply_queries_by_score = multiply_queries_by_score

        self.light_attn = LocalMHA(
            dim = dim,
            dim_head = light_dim_head,
            heads = light_heads,
            window_size = light_window_size // 2,
            prenorm = True,
            causal = False,
            use_rotary_pos_emb = False,
            look_backward = 1,
            look_forward = 1
        )

        self.null_q_token = None
        if use_null_q_tokens:
            self.null_q_token = nn.Parameter(torch.randn(dim)) # for the query tokens not selected by the router, give it a learned output embed

        self.q_router = CoordinateDescentRouter(
            dim = dim,
            straight_through = router_straight_through,
            **router_kwargs
        )

        self.kv_router = CoordinateDescentRouter(
            dim = dim,
            num_routing_tokens = num_routed_kv,
            straight_through = router_straight_through,
            **router_kwargs
        )

        self.heavy_attn = Attention(
            dim = dim,
            dim_head = heavy_dim_head,
            heads = heavy_heads,
            multiply_keys_by_score = multiply_keys_by_score,
            use_flash = use_flash_attn
        )

    def forward(
        self,
        x,
        *,
        num_heavy_tokens_q = None,
        num_heavy_tokens_kv = None,
        mask = None
    ):
        batch, device = x.shape[0], x.device

        num_heavy_tokens_q = default(num_heavy_tokens_q, self.num_heavy_tokens_q)
        num_heavy_tokens_kv = default(num_heavy_tokens_kv, self.num_heavy_tokens_kv)


        # light local attention sees all tokens in a limited context

        light_out = self.light_attn(x, mask = mask)

        # route tokens appropriately for heavy branch

        normalized_scores_q, indices_q = self.q_router(x, num_tokens = num_heavy_tokens_q, mask = mask)
        normalized_scores_kv, indices_kv = self.kv_router(x, num_tokens = num_heavy_tokens_kv, mask = mask)

        # select the tokens to be routed to full attention

        routed_tokens_q = batched_gather(x, indices_q)

        kv_batch_range = create_batch_range(x, right_pad_dims = indices_kv.ndim - 1)
        routed_tokens_kv = batched_gather(x, indices_kv)

        # calculate key padding mask

        routed_tokens_kv_mask = None
        if exists(mask):
            routed_tokens_kv_mask = mask[kv_batch_range, indices_kv]

        # do the heavier branch with only routed tokens

        routed_tokens_out = self.heavy_attn(
            routed_tokens_q,
            mask = routed_tokens_kv_mask,
            context = routed_tokens_kv,
            normalized_scores_kv = normalized_scores_kv,
            normalized_scores_q = normalized_scores_q if self.multiply_queries_by_score else None
        )

        routed_tokens_out = routed_tokens_out * rearrange(normalized_scores_q, '... -> ... 1')

        # scatter back the output of the heavy branch

        if exists(self.null_q_token):
            heavy_out = rearrange(self.null_q_token, 'd -> 1 1 d')
            heavy_out = heavy_out.expand_as(x).clone()
        else:
            heavy_out = torch.zeros_like(x)

        heavy_out = self.q_router.route_back(heavy_out, routed_tokens_out, indices_q)

        # sum light and heavy branches

        return light_out + heavy_out



# improvised conditionally routed autoregressive attention



# class ConditionalMultiHeadRoutedAttention(nn.Module):
#     def __init__(
#         self,
#         dim,
#         *,
#         num_heavy_tokens_q,
#         num_heavy_tokens_kv,
#         num_routed_kv = 1,
#         light_dim_head = 64,
#         light_heads = 8,
#         light_window_size = 128,        # each token would see ~ 64 tokens either way to left or right
#         heavy_dim_head = 64,
#         heavy_heads = 8,
#         router_straight_through = True, # would make sure all normalized scores are 1., still differentiable
#         router_kwargs: dict = {},
#         multiply_keys_by_score = False,
#         multiply_queries_by_score = False,
#         use_triton = False,
#         use_null_q_tokens = True,
#         use_flash_attn = False,
#         # use_alibi = False,
#         # use_xpos = False,
#         # xpos_head_dim = None,
#         # xpos_scale_base = 512,
#         # multiway=False,
#         # use_relative_position_bias=False,
#     ):
#         super().__init__()

#         if use_triton:
#             router_kwargs = {**router_kwargs, 'use_triton': True}

        
#         # #alibi
#         # self.use_alibi = use_alibi

#         # if use_alibi:
#         #     if not alibi_heads:
#         #         alibi_heads = light_heads
#         #     self.alibi_bias = LearnedAlibiPositionalBias(alibi_heads)
#         # #alibi

#         # self.use_xpos = use_xpos
#         # if use_xpos:
#         #     if not xpos_head_dim:
#         #         xpos_head_dim = dim // light_heads
#         #     self.xpos = XPOS(xpos_head_dim, scale_base=xpos_scale_base)

#         # #relative position bias
#         # self.use_relative_position_bias = use_relative_position_bias
#         # if use_relative_position_bias:
#         #     self.relative_position_bias = RelativePositionBias()

#         self.num_heavy_tokens_q = num_heavy_tokens_q
#         self.num_heavy_tokens_kv = num_heavy_tokens_kv

#         self.multiply_queries_by_score = multiply_queries_by_score

#         self.light_attn = MultiwayWrapper(
#             {"multiway": multiway},
#             LocalMHA(
#                 dim=dim,
#                 dim_head=light_dim_head,
#                 heads=light_heads,
#                 window_size=light_window_size // 2,
#                 prenorm=True,
#                 causal=False,
#                 use_rotary_pos_emb=True,
#                 look_backward=1,
#                 look_forward=1,
#             ),
#         )


#         self.null_q_token = None
#         if use_null_q_tokens:
#             self.null_q_token = nn.Parameter(torch.randn(dim)) # for the query tokens not selected by the router, give it a learned output embed

#         self.q_router = CoordinateDescentRouter(
#             dim = dim,
#             straight_through = router_straight_through,
#             **router_kwargs
#         )

#         self.kv_router = CoordinateDescentRouter(
#             dim = dim,
#             num_routing_tokens = num_routed_kv,
#             straight_through = router_straight_through,
#             **router_kwargs
#         )

#         self.heavy_attn = Attention(
#             dim = dim,
#             dim_head = heavy_dim_head,
#             heads = heavy_heads,
#             multiply_keys_by_score = multiply_keys_by_score,
#             use_flash = use_flash_attn
#         )


#     def forward(
#         self,
#         x,
#         *,
#         num_heavy_tokens_q = None,
#         num_heavy_tokens_kv = None,
#         mask = None
#     ):
#         batch, device = x.shape[0], x.device

#         num_heavy_tokens_q = default(num_heavy_tokens_q, self.num_heavy_tokens_q)
#         num_heavy_tokens_kv = default(num_heavy_tokens_kv, self.num_heavy_tokens_kv)


#         # light local attention sees all tokens in a limited context

#         light_out = self.light_attn(x, mask = mask)

#         #use alibi

#         if self.use_alibi:
#             qk_dots = light_out
#             light_out = self.alibi_bias(qk_dots)
#         #=-===============alibi end

#         if self.use_xpos:
#             light_out = self.xpos(light_out)

#         # route tokens appropriately for heavy branch

#         normalized_scores_q, indices_q = self.q_router(x, num_tokens = num_heavy_tokens_q, mask = mask)
#         normalized_scores_kv, indices_kv = self.kv_router(x, num_tokens = num_heavy_tokens_kv, mask = mask)

#         # select the tokens to be routed to full attention

#         routed_tokens_q = batched_gather(x, indices_q)

#         kv_batch_range = create_batch_range(x, right_pad_dims = indices_kv.ndim - 1)
#         routed_tokens_kv = batched_gather(x, indices_kv)

#         # calculate key padding mask

#         routed_tokens_kv_mask = None
#         if exists(mask):
#             routed_tokens_kv_mask = mask[kv_batch_range, indices_kv]

#         # do the heavier branch with only routed tokens

#         routed_tokens_out = self.heavy_attn(
#             routed_tokens_q,
#             mask = routed_tokens_kv_mask,
#             context = routed_tokens_kv,
#             normalized_scores_kv = normalized_scores_kv,
#             normalized_scores_q = normalized_scores_q if self.multiply_queries_by_score else None
#         )

#         routed_tokens_out = routed_tokens_out * rearrange(normalized_scores_q, '... -> ... 1')

#         # scatter back the output of the heavy branch

#         if exists(self.null_q_token):
#             heavy_out = rearrange(self.null_q_token, 'd -> 1 1 d')
#             heavy_out = heavy_out.expand_as(x).clone()
#         else:
#             heavy_out = torch.zeros_like(x)

#         heavy_out = self.q_router.route_back(heavy_out, routed_tokens_out, indices_q)

#         # sum light and heavy branches

#         return light_out + heavy_out












# improvised conditionally routed autoregressive attention

class ConditionalRoutedAutoregressiveAttention(nn.Module):
    def __init__(
        self,
        dim,
        *,
        num_heavy_tokens_q,
        num_heavy_tokens_kv,
        num_routed_kv = 1,
        light_dim_head = 64,
        light_heads = 8,
        light_window_size = 128,        # each token would see ~ 64 tokens either way to left or right
        heavy_window_size = None,
        heavy_dim_head = 64,
        heavy_heads = 8,
        router_straight_through = True, # would make sure all normalized scores are 1., still differentiable
        router_kwargs: dict = {},
        multiply_keys_by_score = False,
        multiply_queries_by_score = False,
        use_triton = False,
        use_null_q_tokens = True,
        use_flash_attn = False,
        use_alibi = False
    ):
        super().__init__()

        if use_triton:
            router_kwargs = {**router_kwargs, 'use_triton': True}

        if use_alibi:
            self.light_alibi_bias = LearnedAlibiPositionalBias(light_heads)
            self.heavy_alibi_bias = LearnedAlibiPositionalBias(heavy_heads)
        else:
            self.light_alibi_bias = None
            self.heavy_alibi_bias = None

        self.num_heavy_tokens_q = num_heavy_tokens_q
        self.num_heavy_tokens_kv = num_heavy_tokens_kv

        self.multiply_queries_by_score = multiply_queries_by_score

        self.heavy_window_size = default(heavy_window_size, light_window_size)

        self.light_attn = LocalMHA(
            dim = dim,
            dim_head = light_dim_head,
            heads = light_heads,
            window_size = light_window_size,
            prenorm = True,
            causal = True,
            exact_windowsize = False,
            use_rotary_pos_emb = True
        )

        self.null_q_token = None
        if use_null_q_tokens:
            self.null_q_token = nn.Parameter(torch.randn(dim)) # for the query tokens not selected by the router, give it a learned output embed

        self.q_router = CoordinateDescentRouter(
            dim = dim,
            straight_through = router_straight_through,
            **router_kwargs
        )

        self.kv_router = CoordinateDescentRouter(
            dim = dim,
            num_routing_tokens = num_routed_kv,
            straight_through = router_straight_through,
            **router_kwargs
        )

        self.heavy_attn = Attention(
            dim = dim,
            dim_head = heavy_dim_head,
            heads = heavy_heads,
            multiply_keys_by_score = multiply_keys_by_score,
            use_flash = use_flash_attn
        )

        # self.light_alibi_bias = LearnedAlibiPositionalBias(light_heads)
        # self.heavy_alibi_bias = LearnedAlibiPositionalBias(heavy_heads)

        if use_alibi:
            self.light_alibi_bias = LearnedAlibiPositionalBias(light_heads)
            self.heavy_alibi_bias = LearnedAlibiPositionalBias(heavy_heads)
        else:
            self.light_alibi_bias = None
            self.heavy_alibi_bias = None

    def forward(
        self,
        x,
        *,
        num_heavy_tokens_q = None,
        num_heavy_tokens_kv = None
    ):
        batch, device = x.shape[0], x.device

        num_heavy_tokens_q = default(num_heavy_tokens_q, self.num_heavy_tokens_q)
        num_heavy_tokens_kv = default(num_heavy_tokens_kv, self.num_heavy_tokens_kv)

        # light local attention sees all tokens in a limited context

        light_out = self.light_attn(x)

        # pad sequence to multiple of the heavy window size
        # routing will take place within each heavy window block size

        window_size = self.heavy_window_size

        x, seq_len = pad_to_multiple(x, window_size, dim = -2)

        padded_seq_len = x.shape[-2]

        # construct mask, and make sure not to attend to padding

        q_mask = torch.ones((batch, seq_len), dtype = torch.bool, device = device)
        q_mask = F.pad(q_mask, (0, padded_seq_len - seq_len), value = False)

        # handy function

        merge_to_batch = lambda t: rearrange(t, 'b n ... -> (b n) ...')

        # block the sequence and mask into windows for the queries

        q = rearrange(x, 'b (n w) d -> b n w d', w = window_size)
        q_mask = rearrange(q_mask, 'b (n w) -> b n w', w = window_size)

        q, q_mask = map(merge_to_batch, (q[:, 1:], q_mask[:, 1:]))

        # each block of queries attend to sequences that are causally masked out appropriately

        windows = padded_seq_len // window_size

        kv = repeat(x, 'b n d -> b m n d', m = windows)

        kv_mask = torch.ones((windows, windows), dtype = torch.bool, device = device).tril(-1)
        kv_mask = repeat(kv_mask, 'm n -> b m (n w)', b = batch, w = window_size)

        kv, kv_mask = map(merge_to_batch, (kv[:, 1:], kv_mask[:, 1:]))

        # route tokens appropriately for heavy branch, if need be

        should_route_q = q.shape[-2] > num_heavy_tokens_q
        should_route_kv = kv.shape[-2] > num_heavy_tokens_kv

        if should_route_q:
            normalized_scores_q, indices_q = self.q_router(q, num_tokens = num_heavy_tokens_q, mask = q_mask)

            routed_tokens_q = batched_gather(q, indices_q)
        else:
            normalized_scores_q = 1.
            routed_tokens_q = q

        if should_route_kv:
            normalized_scores_kv, indices_kv = self.kv_router(kv, num_tokens = num_heavy_tokens_kv, mask = kv_mask)

            routed_tokens_kv = batched_gather(kv, indices_kv)
            routed_tokens_kv_mask = batched_gather(kv_mask, indices_kv)
        else:
            normalized_scores_kv = 1.
            routed_tokens_kv = kv
            routed_tokens_kv_mask = kv_mask

        # do the heavier branch with only routed tokens

        routed_tokens_out = self.heavy_attn(
            routed_tokens_q,
            mask = routed_tokens_kv_mask,
            context = routed_tokens_kv,
            normalized_scores_kv = normalized_scores_kv,
            normalized_scores_q = normalized_scores_q if self.multiply_queries_by_score else None
        )

        # if self.light_alibi_bias is not None:
        #     light_qk_dots = light_qk_dots + self.light_alibi_bias(light_qk_dots)
        # if self.heavy_alibi_bias is not None:
        #     heavy_qk_dots = heavy_qk_dots + self.heavy_alibi_bias(heavy_qk_dots)
        if self.light_alibi_bias is not None:
            light_qk_dots = light_qk_dots + self.light_alibi_bias(light_qk_dots)
        if self.heavy_alibi_bias is not None:
            heavy_qk_dots = heavy_qk_dots + self.heavy_alibi_bias(heavy_qk_dots)

        if should_route_q:
            routed_tokens_out = routed_tokens_out * rearrange(normalized_scores_q, '... -> ... 1')

            # scatter back the output of the heavy branch

            if exists(self.null_q_token):
                heavy_out = rearrange(self.null_q_token, 'd -> 1 1 d')
                heavy_out = heavy_out.expand_as(q).clone()
            else:
                heavy_out = torch.zeros_like(q)

            heavy_out = self.q_router.route_back(heavy_out, routed_tokens_out, indices_q)
        else:
            heavy_out = routed_tokens_out

        # un-window and slice out original sequence

        heavy_out = rearrange(heavy_out, '(b n) w d -> b (n w) d', b = batch)
        heavy_out = heavy_out[:, :(seq_len - window_size)]

        heavy_out = F.pad(heavy_out, (0, 0, window_size, 0), value = 0.)

        # sum light and heavy branches

        return light_out + heavy_out

# adapting the conditional routed self attention to cross attention

class ConditionalRoutedCrossAttention(nn.Module):
    def __init__(
        self,
        dim,
        *,
        num_tokens_q,
        num_tokens_kv,
        num_sets_kv = 1,                # setting this greater than 1 would route multiple sets of key / values, each of size num_tokens_kv, using this many routing tokens
        dim_head = 64,
        heads = 8,
        router_straight_through = True, # would make sure all normalized scores are 1., still differentiable
        router_kwargs: dict = {},
        kv_routing_tokens = 1,
        multiply_keys_by_score = False,
        use_triton = False,
        use_null_q_tokens = True,
        use_flash_attn = False,
        route_block_size = None
    ):
        super().__init__()

        if use_triton:
            router_kwargs = {**router_kwargs, 'use_triton': True}

        self.num_tokens_q = num_tokens_q
        self.num_tokens_kv = num_tokens_kv

        self.null_q_token = None
        if use_null_q_tokens:
            self.null_q_token = nn.Parameter(torch.randn(dim)) # for the query tokens not selected by the router, give it a learned output embed

        self.q_router = CoordinateDescentRouter(
            dim = dim,
            straight_through = router_straight_through,
            **router_kwargs
        )

        self.kv_router = CoordinateDescentRouter(
            dim = dim,
            straight_through = router_straight_through,
            num_routing_tokens = kv_routing_tokens,
            route_block_size = route_block_size,
            **router_kwargs
        )

        self.heavy_attn = Attention(
            dim = dim,
            dim_head = dim_head,
            heads = heads,
            multiply_keys_by_score = multiply_keys_by_score,
            use_flash = use_flash_attn
        )

    def forward(
        self,
        x,
        context,
        *,
        num_tokens_q = None,
        num_tokens_kv = None,
        mask = None,
        context_mask = None
    ):
        batch, device = x.shape[0], x.device

        # route the queries

        query_length = x.shape[-2]
        num_tokens_q = default(num_tokens_q, self.num_tokens_q)

        routed_tokens_q = x
        should_route_queries = query_length > num_tokens_q

        if should_route_queries:
            normalized_scores_q, indices_q = self.q_router(x, num_tokens = num_tokens_q, mask = mask)

            routed_tokens_q = batched_gather(x, indices_q)

        # route the long contexts

        key_value_length = context.shape[-2]
        num_tokens_kv = default(num_tokens_kv, self.num_tokens_kv)

        routed_tokens_kv = context
        routed_tokens_kv_mask = context_mask
        normalized_scores_kv = None

        should_route_kv = key_value_length > num_tokens_kv

        if should_route_kv:
            normalized_scores_kv, indices_kv = self.kv_router(context, num_tokens = num_tokens_kv, mask = context_mask)

            routed_tokens_kv = batched_gather(context, indices_kv)

            routed_tokens_kv_mask = None
            if exists(context_mask):
                routed_tokens_kv_mask = batched_gather(context_mask, indices_kv)

        # do the heavier branch with only routed tokens

        routed_tokens_out = self.heavy_attn(
            routed_tokens_q,
            mask = routed_tokens_kv_mask,
            context = routed_tokens_kv,
            normalized_scores_kv = normalized_scores_kv
        )

        if should_route_queries:
            routed_tokens_out = routed_tokens_out * rearrange(normalized_scores_q, '... -> ... 1')

        # early return if queries did not undergo routing

        if not should_route_queries:
            return routed_tokens_out

        # otherwise, scatter back the query outputs

        if exists(self.null_q_token):
            out = rearrange(self.null_q_token, 'd -> 1 1 d')
            out = out.expand_as(x).clone()
        else:
            out = torch.zeros_like(x)

        out = self.q_router.route_back(out, routed_tokens_out, indices_q)

        return out

# block


# light and heavy attention modules with the MultiwayNetwork. Here's the modified version of the ConditionalRoutedAttention class:

class MultiModalityConditionalAttention(ConditionalRoutedAttention):
    def __init__(self, *args, multiway: bool=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.multiway = multiway
        if self.multiway:
            self.light_attn = MultiwayWrapper(args, self.light_attn)
            self.heavy_attn = MultiwayWrapper(args, self.heavy_attn)
            self.q_router = MultiwayWrapper(args, self.q_router)
            self.kv_router = MultiwayWrapper(args, self.kv_router)

    def forward(
            self,
            x,
            *,
            num_heavy_tokens_q=None,
            num_heavy_tokens_kv=None,
            mask=None,
            set_split_position=None,
        ):
        if self.multiway and set_split_position is not None:
            self.light_attn.apply(set_split_position(set_split_position))
            self.heavy_attn.apply(set_split_position(set_split_position))
            self.q_router.apply(set_split_position(set_split_position))
            self.kv_router.apply(set_split_position(set_split_position))
        
        return super().forward(
            x,
            num_heavy_tokens_q=num_heavy_tokens_q,
            num_heavy_tokens_kv=num_heavy_tokens_kv,
            mask=mask
        )


# class ConditionalRoutedTransformerBlock(nn.Module):
#     def __init__(
#         self,
#         dim,
#         *,
#         num_heavy_attn_tokens_q,
#         num_heavy_attn_tokens_kv,
#         num_routed_kv = 1,
#         num_heavy_ff_tokens,
#         light_dim_head = 64,
#         light_heads = 8,
#         light_window_size = 128,
#         heavy_dim_head = 64,
#         heavy_heads = 8,
#         light_ff_mult = 0.5,
#         heavy_ff_mult = 4,
#         router_straight_through = True,
#         router_kwargs: dict = {},
#         multiply_keys_by_score = False,
#         multiply_queries_by_score = False,
#         use_triton = False,
#         use_null_q_tokens = True,
#         use_flash_attn = False,
#         use_alibi = False,
#         alibi_heads = None,
#         use_xpos = False,
#         xpos_head_dim = None,
#         xpos_scale_base = 512,
#     ):
#         super().__init__()
#         self.conditional_ff = ConditionalRoutedFeedForward(
#             dim,
#             num_heavy_tokens = num_heavy_ff_tokens,
#             light_ff_mult = light_ff_mult,
#             heavy_ff_mult = heavy_ff_mult,
#             router_straight_through = router_straight_through,
#             router_kwargs = router_kwargs,
#             use_triton = use_triton
#         )

#         self.conditional_attn = ConditionalRoutedAttention(
#             dim,
#             light_dim_head = light_dim_head,
#             light_heads = light_heads,
#             light_window_size = light_window_size,
#             heavy_dim_head = heavy_dim_head,
#             heavy_heads = heavy_heads,
#             num_heavy_tokens_q = num_heavy_attn_tokens_q,
#             num_heavy_tokens_kv = num_heavy_attn_tokens_kv,
#             num_routed_kv = num_routed_kv,
#             router_straight_through = router_straight_through,
#             router_kwargs = router_kwargs,
#             multiply_keys_by_score = multiply_keys_by_score,
#             multiply_queries_by_score = multiply_queries_by_score,
#             use_triton = use_triton,
#             use_null_q_tokens = use_null_q_tokens,
#             use_flash_attn = use_flash_attn,
#             use_alibi = use_alibi,
#             use_xpos = use_xpos,
#             xpos_head_dim = xpos_head_dim,
#             xpos_scale_base = xpos_scale_base
#         )

#     def forward(
#         self,
#         x,
#         mask = None,
#         num_heavy_attn_tokens_q = None,
#         num_heavy_attn_tokens_kv = None,
#         num_heavy_ff_tokens = None
#     ):
#         x = self.conditional_attn(x, mask = mask, num_heavy_tokens_q = num_heavy_attn_tokens_q, num_heavy_tokens_kv = num_heavy_attn_tokens_kv) + x
#         x = self.conditional_ff(x, mask = mask, num_heavy_tokens = num_heavy_ff_tokens) + x
#         return x


class ConditionalRoutedTransformerBlock(nn.Module):
    def __init__(
        self,
        dim,
        *,
        num_heavy_attn_tokens_q,
        num_heavy_attn_tokens_kv,
        num_routed_kv = 1,
        num_heavy_ff_tokens,
        light_dim_head = 64,
        light_heads = 8,
        light_window_size = 128,
        heavy_dim_head = 64,
        heavy_heads = 8,
        light_ff_mult = 0.5,
        heavy_ff_mult = 4,
        router_straight_through = True,
        router_kwargs: dict = {},
        multiply_keys_by_score = False,
        multiply_queries_by_score = False,
        use_triton = False,
        use_null_q_tokens = True,
        use_flash_attn = False
    ):
        super().__init__()
        self.conditional_ff = ConditionalRoutedFeedForward(
            dim,
            num_heavy_tokens = num_heavy_ff_tokens,
            light_ff_mult = light_ff_mult,
            heavy_ff_mult = heavy_ff_mult,
            router_straight_through = router_straight_through,
            router_kwargs = router_kwargs,
            use_triton = use_triton
        )

        self.conditional_attn = ConditionalRoutedAttention(
            dim,
            light_dim_head = light_dim_head,
            light_heads = light_heads,
            light_window_size = light_window_size,
            heavy_dim_head = heavy_dim_head,
            heavy_heads = heavy_heads,
            num_heavy_tokens_q = num_heavy_attn_tokens_q,
            num_heavy_tokens_kv = num_heavy_attn_tokens_kv,
            num_routed_kv = num_routed_kv,
            router_straight_through = router_straight_through,
            router_kwargs = router_kwargs,
            multiply_keys_by_score = multiply_keys_by_score,
            multiply_queries_by_score = multiply_queries_by_score,
            use_triton = use_triton,
            use_null_q_tokens = use_null_q_tokens,
            use_flash_attn = use_flash_attn
        )

    def forward(
        self,
        x,
        mask = None,
        num_heavy_attn_tokens_q = None,
        num_heavy_attn_tokens_kv = None,
        num_heavy_ff_tokens = None
    ):
        x = self.conditional_attn(x, mask = mask, num_heavy_tokens_q = num_heavy_attn_tokens_q, num_heavy_tokens_kv = num_heavy_attn_tokens_kv) + x
        x = self.conditional_ff(x, mask = mask, num_heavy_tokens = num_heavy_ff_tokens) + x
        return x



class Andromeda(nn.Module):
    def __init__(
        self,
        dim,
        *,
        num_heavy_attn_tokens_q,
        num_heavy_attn_tokens_kv,
        num_routed_kv = 1,
        num_heavy_ff_tokens,
        light_dim_head = 64,
        light_heads = 8,
        light_window_size = 128,
        heavy_dim_head = 64,
        heavy_heads = 8,
        light_ff_mult = 0.5,
        heavy_ff_mult = 4,
        router_straight_through = True,
        router_kwargs: dict = {},
        multiply_keys_by_score = False,
        multiply_queries_by_score = False,
        use_triton = False,
        use_null_q_tokens = True,
        use_flash_attn = False,
        use_alibi = False
    ):
        super().__init__()
        self.conditional_ff = ConditionalRoutedFeedForward(
            dim,
            num_heavy_tokens = num_heavy_ff_tokens,
            light_ff_mult = light_ff_mult,
            heavy_ff_mult = heavy_ff_mult,
            router_straight_through = router_straight_through,
            router_kwargs = router_kwargs,
            use_triton = use_triton
        )

        # no xpos on routed autoregressive

        self.conditional_attn = ConditionalRoutedAutoregressiveAttention(
            dim,
            light_dim_head = light_dim_head,
            light_heads = light_heads,
            light_window_size = light_window_size,
            heavy_dim_head = heavy_dim_head,
            heavy_heads = heavy_heads,
            num_heavy_tokens_q = num_heavy_attn_tokens_q,
            num_heavy_tokens_kv = num_heavy_attn_tokens_kv,
            num_routed_kv = num_routed_kv,
            router_straight_through = router_straight_through,
            router_kwargs = router_kwargs,
            multiply_keys_by_score = multiply_keys_by_score,
            multiply_queries_by_score = multiply_queries_by_score,
            use_triton = use_triton,
            use_null_q_tokens = use_null_q_tokens,
            use_flash_attn = use_flash_attn,
            use_alibi=use_alibi
        )

    def forward(
        self,
        x,
        mask = None,
        num_heavy_attn_tokens_q = None,
        num_heavy_attn_tokens_kv = None,
        num_heavy_ff_tokens = None
    ):
        x = self.conditional_attn(x, mask = mask, num_heavy_tokens_q = num_heavy_attn_tokens_q, num_heavy_tokens_kv = num_heavy_attn_tokens_kv) + x
        x = self.conditional_ff(x, mask = mask, num_heavy_tokens = num_heavy_ff_tokens) + x
        return x

