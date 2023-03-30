from typing import List, Optional, Union, Tuple
import math

import torch
from e3nn import o3
from e3nn.util.jit import compile_mode

from diffusion_edf.equiformer.drop import GraphDropPath, EquivariantDropout
from diffusion_edf.equiformer.tensor_product_rescale import FullyConnectedTensorProductRescale, LinearRS, FullyConnectedTensorProductRescaleSwishGate
from diffusion_edf.equiformer.layer_norm import EquivariantLayerNormV2
from diffusion_edf.equiformer.graph_attention_transformer import sort_irreps_even_first

from diffusion_edf.graph_attention import GraphAttentionMLP
from diffusion_edf.connectivity import FpsPool, RadiusGraph, RadiusConnect
from diffusion_edf.radial_func import GaussianRadialBasisLayerFiniteCutoff


@compile_mode('script')
class FeedForwardNetwork(torch.nn.Module):
    '''
        Use two (FCTP + Gate)
    '''
    def __init__(self,
        irreps_node_input: o3.Irreps,
        irreps_node_output: o3.Irreps, 
        irreps_mlp_mid: Optional[o3.Irreps] = None,
        proj_drop: float = 0.1, bias: bool = True, rescale: bool = True):
        
        super().__init__()
        self.irreps_node_input: o3.Irreps = o3.Irreps(irreps_node_input)
        self.irreps_mlp_mid: o3.Irreps = o3.Irreps(irreps_mlp_mid) if irreps_mlp_mid is not None \
            else self.irreps_node_input
        self.irreps_node_output: o3.Irreps = o3.Irreps(irreps_node_output)
        self.irreps_node_attr = o3.Irreps("1x0e")
        
        self.fctp_1 = FullyConnectedTensorProductRescaleSwishGate(
            self.irreps_node_input, self.irreps_node_attr, self.irreps_mlp_mid, 
            bias=bias, rescale=rescale)
        self.fctp_2 = FullyConnectedTensorProductRescale(
            self.irreps_mlp_mid, self.irreps_node_attr, self.irreps_node_output, 
            bias=bias, rescale=rescale)
        
        if not proj_drop:
            self.proj_drop = None
        else:
            self.proj_drop = EquivariantDropout(self.irreps_node_output, drop_prob=proj_drop)
            
        
    def forward(self, node_input: torch.Tensor) -> torch.Tensor:
        node_attr = torch.ones_like(node_input[:, 0:1])
        node_output: torch.Tensor = self.fctp_1(node_input, node_attr)
        node_output: torch.Tensor = self.fctp_2(node_output, node_attr)
        if self.proj_drop is not None:
            node_output: torch.Tensor = self.proj_drop(node_output)
        return node_output






@compile_mode('script')
class EquiformerBlock(torch.nn.Module):  
    def __init__(self,
        irreps_src: o3.Irreps, 
        irreps_dst: o3.Irreps, 
        irreps_edge_attr: o3.Irreps, 
        irreps_head: o3.Irreps,
        num_heads: int, 
        fc_neurons: List[int],
        irreps_mlp_mid: Union[o3.Irreps, int] = 3,
        attn_type: str = 'mlp',
        alpha_drop: float = 0.1,
        proj_drop: float = 0.1,
        drop_path_rate: float = 0.0,
        src_bias: bool = False,
        dst_bias: bool = True):
        
        super().__init__()
        self.irreps_src: o3.Irreps = o3.Irreps(irreps_src)
        self.irreps_dst: o3.Irreps = o3.Irreps(irreps_dst)
        self.irreps_edge_attr: o3.Irreps = o3.Irreps(irreps_edge_attr)
        self.irreps_head: o3.Irreps = o3.Irreps(irreps_head)
        self.num_heads: int = num_heads
        self.fc_neurons: List[int] = fc_neurons

        self.irreps_emb: o3.Irreps = self.irreps_dst
        assert num_heads*self.irreps_head.dim == self.irreps_emb.dim
        if isinstance(irreps_mlp_mid, o3.Irreps):
            self.irreps_mlp_mid: o3.Irreps = o3.Irreps(irreps_mlp_mid)
        elif isinstance(irreps_mlp_mid, int):
            self.irreps_mlp_mid = sort_irreps_even_first((self.irreps_emb * irreps_mlp_mid))[0].simplify()

        self.norm_1_src = EquivariantLayerNormV2(self.irreps_src)
        self.norm_1_dst = EquivariantLayerNormV2(self.irreps_dst)

        self.linear_src = LinearRS(self.irreps_src, self.irreps_emb, bias=src_bias)
        self.linear_dst = LinearRS(self.irreps_dst, self.irreps_emb, bias=dst_bias)


        if attn_type not in ['mlp', 'linear', 'dp']:
            raise ValueError(f"Unknown attention type: {attn_type}")
        self.attn_type: str = attn_type
        if self.attn_type == 'mlp':
            self.ga = GraphAttentionMLP(irreps_emb = self.irreps_emb,
                                        irreps_edge_attr = self.irreps_edge_attr,
                                        irreps_node_output = self.irreps_dst,
                                        fc_neurons = self.fc_neurons,
                                        irreps_head = self.irreps_head,
                                        num_heads=self.num_heads, 
                                        alpha_drop=alpha_drop, 
                                        proj_drop=proj_drop)
        elif self.attn_type == 'linear':
            raise NotImplementedError
        elif self.attn_type == 'dp':
            raise NotImplementedError
        else:
            raise ValueError(f"Unknown attention type: {self.attn_type}")
        

        self.drop_path = GraphDropPath(drop_path_rate) if drop_path_rate > 0. else None
        self.norm_2 = EquivariantLayerNormV2(self.irreps_dst)
        self.ffn = FeedForwardNetwork(
            irreps_node_input=self.irreps_dst, 
            irreps_node_output=self.irreps_dst, 
            irreps_mlp_mid=self.irreps_mlp_mid,
            proj_drop=proj_drop)
            
    def forward(self, node_input_src: torch.Tensor,
                node_input_dst: torch.Tensor,
                batch_dst: torch.Tensor,
                edge_src: torch.Tensor,
                edge_dst: torch.Tensor,
                edge_attr: torch.Tensor,
                edge_scalars: torch.Tensor) -> torch.Tensor:

        message_src: torch.Tensor = self.norm_1_src(node_input_src)
        message_src: torch.Tensor = self.linear_src(node_input_src)

        message_dst: torch.Tensor = self.norm_1_dst(node_input_dst)
        message_dst: torch.Tensor = self.linear_dst(node_input_dst)

        message: torch.Tensor = message_src[edge_src] + message_dst[edge_dst]
        
        node_features: torch.Tensor = self.ga(message=message, 
                                              edge_dst=edge_dst, 
                                              edge_attr=edge_attr, 
                                              edge_scalars=edge_scalars,
                                              n_nodes_dst = len(node_input_dst))
        
        if self.drop_path is not None:
            node_features = self.drop_path(node_features, batch_dst)
        node_output: torch.Tensor = node_input_dst + node_features # skip connection
        
        node_features: torch.Tensor = self.norm_2(node_output, batch=batch_dst)
        node_features: torch.Tensor = self.ffn(node_features)
        
        if self.drop_path is not None:
            node_features = self.drop_path(node_features, batch_dst)
        node_output = node_output + node_features
        
        return node_output





@compile_mode('script')
class PoolingBlock(torch.nn.Module):  
    def __init__(self,
        irreps_src: o3.Irreps, 
        irreps_dst: o3.Irreps, 
        irreps_edge_attr: o3.Irreps, 
        irreps_head: o3.Irreps,
        num_heads: int, 
        fc_neurons: List[int],
        pool_radius: float,
        pool_ratio: float,
        pool_method: str = 'fps',
        deterministic: bool = False,
        irreps_mlp_mid: Union[o3.Irreps, int] = 3,
        attn_type: str = 'mlp',
        alpha_drop: float = 0.1,
        proj_drop: float = 0.1,
        drop_path_rate: float = 0.0):
        
        super().__init__()
        self.irreps_src: o3.Irreps = o3.Irreps(irreps_src)
        self.irreps_dst: o3.Irreps = o3.Irreps(irreps_dst)
        self.irreps_edge_attr: o3.Irreps = o3.Irreps(irreps_edge_attr)
        self.irreps_head: o3.Irreps = o3.Irreps(irreps_head)
        self.num_heads: int = num_heads
        self.fc_neurons: List[int] = fc_neurons

        if self.irreps_src != self.irreps_dst:
            raise NotImplementedError

        self.block = EquiformerBlock(irreps_src = self.irreps_src, 
                                     irreps_dst = self.irreps_dst, 
                                     irreps_edge_attr = self.irreps_edge_attr, 
                                     irreps_head = self.irreps_head,
                                     num_heads = self.num_heads, 
                                     fc_neurons = self.fc_neurons,
                                     irreps_mlp_mid = irreps_mlp_mid,
                                     attn_type = attn_type,
                                     alpha_drop = alpha_drop,
                                     proj_drop = proj_drop,
                                     drop_path_rate = drop_path_rate,
                                     src_bias = False,
                                     dst_bias = True)
        
        self.pool_radius: float = pool_radius
        self.pool_ratio: float = pool_ratio
        assert isinstance(pool_method, str), f"Unknown pooling method: {pool_method}"
        if pool_method == 'fps':
            self.pool_layer = FpsPool(ratio=self.pool_ratio, random_start=not deterministic, r=self.pool_radius, max_num_neighbors=1000)
        else:
            raise ValueError(f"Unknown pooling method: {pool_method}")


        assert len(fc_neurons) >= 1
        self.num_radial_basis = fc_neurons[0]
        self.radial_basis_fn = GaussianRadialBasisLayerFiniteCutoff(num_basis=self.num_radial_basis, cutoff=self.pool_radius * 0.99)

        self.spherical_harmonics = o3.SphericalHarmonics(irreps_out = self.irreps_edge_attr, normalize = True, normalization='component')

    def forward(self, node_feature: torch.Tensor,
                node_coord: torch.Tensor,
                batch: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:

        node_feature_dst, node_coord_dst, edge_src, edge_dst, degree, batch_dst = self.pool_layer(node_coord_src = node_coord, 
                                                                                                  node_feature_src = node_feature, 
                                                                                                  batch_src = batch)
        
        edge_vec: torch.Tensor = node_coord.index_select(0, edge_src) - node_coord_dst.index_select(0, edge_dst)
        edge_attr = self.spherical_harmonics(edge_vec)
        edge_length = edge_vec.norm(dim=1, p=2)
        edge_scalars = self.radial_basis_fn(edge_length)

        node_feature_dst = self.block(node_input_src = node_feature,
                                      node_input_dst = node_feature_dst,
                                      batch_dst = batch_dst,
                                      edge_src = edge_src,
                                      edge_dst = edge_dst,
                                      edge_attr = edge_attr,
                                      edge_scalars = edge_scalars)
        
        
        return node_feature_dst, node_coord_dst, \
               edge_src, edge_dst, edge_length, edge_attr, \
               degree, batch_dst






@compile_mode('script')
class RadiusGraphBlock(torch.nn.Module):  
    def __init__(self,
        irreps: o3.Irreps, 
        irreps_edge_attr: o3.Irreps, 
        irreps_head: o3.Irreps,
        num_heads: int, 
        fc_neurons: List[int],
        radius: float,
        n_layers: int,
        irreps_mlp_mid: Union[o3.Irreps, int] = 3,
        attn_type: str = 'mlp',
        alpha_drop: float = 0.1,
        proj_drop: float = 0.1,
        drop_path_rate: float = 0.0):
        
        super().__init__()
        self.irreps: o3.Irreps = o3.Irreps(irreps)
        self.irreps_edge_attr: o3.Irreps = o3.Irreps(irreps_edge_attr)
        self.irreps_head: o3.Irreps = o3.Irreps(irreps_head)
        self.num_heads: int = num_heads
        self.fc_neurons: List[int] = fc_neurons
        self.r: float = radius
        assert len(fc_neurons) >= 1
        self.num_radial_basis: int = fc_neurons[0]
        self.n_layers: int = n_layers
        assert self.n_layers >= 1




        self.radius_graph = RadiusGraph(r=self.r, max_num_neighbors=1000)

        self.blocks = torch.nn.ModuleList([
            EquiformerBlock(irreps_src = self.irreps, 
                            irreps_dst = self.irreps, 
                            irreps_edge_attr = self.irreps_edge_attr, 
                            irreps_head = self.irreps_head,
                            num_heads = self.num_heads, 
                            fc_neurons = self.fc_neurons,
                            irreps_mlp_mid = irreps_mlp_mid,
                            attn_type = attn_type,
                            alpha_drop = alpha_drop,
                            proj_drop = proj_drop,
                            drop_path_rate = drop_path_rate,
                            src_bias = False,
                            dst_bias = True) for _ in range(self.n_layers)
        ])

        self.radials = torch.nn.ModuleList([
            GaussianRadialBasisLayerFiniteCutoff(num_basis=self.num_radial_basis, cutoff=self.r * 0.99) for _ in range(self.n_layers)
        ])

        self.spherical_harmonics = o3.SphericalHarmonics(irreps_out = self.irreps_edge_attr, normalize = True, normalization='component')

    def forward(self, node_feature: torch.Tensor,
                node_coord: torch.Tensor,
                batch: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
        
        _, _, edge_src, edge_dst, degree, _ = self.radius_graph(node_coord_src = node_coord, 
                                                                node_feature_src = node_feature, 
                                                                batch_src = batch)
        
        edge_vec: torch.Tensor = node_coord.index_select(0, edge_src) - node_coord.index_select(0, edge_dst)
        edge_attr = self.spherical_harmonics(edge_vec)
        edge_length = edge_vec.norm(dim=1, p=2)

        for block, radial_basis_fn in zip(self.blocks, self.radials):
            edge_scalars = radial_basis_fn(edge_length)

            node_feature = block(node_input_src = node_feature,
                                 node_input_dst = node_feature,
                                 batch_dst = batch,
                                 edge_src = edge_src,
                                 edge_dst = edge_dst,
                                 edge_attr = edge_attr,
                                 edge_scalars = edge_scalars)
        

        return node_feature, node_coord, \
               edge_src, edge_dst, edge_length, edge_attr, \
               degree, batch
    






@compile_mode('script')
class DownBlock(torch.nn.Module):  
    def __init__(self,
        irreps: o3.Irreps, 
        irreps_edge_attr: o3.Irreps, 
        irreps_head: o3.Irreps,
        num_heads: int, 
        fc_neurons: List[int],
        init_radius: float,
        pool_ratio: float,
        n_scales: int,
        n_layers_per_scale: int,
        pool_method: Optional[str] = 'fps',
        deterministic: bool = False,
        irreps_mlp_mid: Union[o3.Irreps, int] = 3,
        attn_type: str = 'mlp',
        alpha_drop: float = 0.1,
        proj_drop: float = 0.1,
        drop_path_rate: float = 0.0):
        
        super().__init__()
        self.irreps: o3.Irreps = o3.Irreps(irreps)
        self.irreps_edge_attr: o3.Irreps = o3.Irreps(irreps_edge_attr)
        self.irreps_head: o3.Irreps = o3.Irreps(irreps_head)
        self.num_heads: int = num_heads
        self.fc_neurons: List[int] = fc_neurons
        self.n_scales: int = n_scales
        self.n_layers_per_scale: int = n_layers_per_scale
        self.pool_ratio: float = pool_ratio
        self.pool_method: Optional[str] = pool_method
        self.deterministic: bool = deterministic
        if self.pool_ratio == 1.0:
            assert self.pool_method is None
        else:
            assert self.pool_method is not None
        assert self.n_layers_per_scale >= 1


        self.layers = torch.nn.ModuleList()
        self.radius_list: List[float] = []
        
        radius = init_radius * math.sqrt(self.pool_ratio)
        for s in range(self.n_scales):
            radius = radius / math.sqrt(self.pool_ratio)
            self.radius_list.append(radius)

            if self.pool_method is not None:
                self.layers.append(
                    PoolingBlock(irreps_src = self.irreps,
                                irreps_dst = self.irreps,
                                irreps_edge_attr = self.irreps_edge_attr,
                                irreps_head = self.irreps_head,
                                num_heads = self.num_heads,
                                fc_neurons = self.fc_neurons,
                                pool_radius = radius,
                                pool_ratio = self.pool_ratio,
                                pool_method = self.pool_method,
                                deterministic = self.deterministic,
                                irreps_mlp_mid = irreps_mlp_mid,
                                attn_type = attn_type,
                                alpha_drop = alpha_drop,
                                proj_drop = proj_drop,
                                drop_path_rate = drop_path_rate)
                )
            
            n_self_connecting_layers = self.n_layers_per_scale - (self.pool_method is not None)
            if n_self_connecting_layers >= 1:
                self.layers.append(
                    RadiusGraphBlock(irreps = self.irreps,
                                     irreps_edge_attr = self.irreps_edge_attr,
                                     irreps_head = self.irreps_head,
                                     num_heads = self.num_heads,
                                     fc_neurons = self.fc_neurons,
                                     radius = radius,
                                     n_layers = n_self_connecting_layers,
                                     irreps_mlp_mid = irreps_mlp_mid,
                                     attn_type = attn_type,
                                     alpha_drop = alpha_drop,
                                     proj_drop = proj_drop,
                                     drop_path_rate = drop_path_rate)
                )



    def forward(self, node_feature: torch.Tensor,
                node_coord: torch.Tensor,
                batch: torch.Tensor) -> List[Tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]]:

        outputs: List[Tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]] = []
        for layer in self.layers:
            output = layer(node_feature = node_feature, node_coord = node_coord, batch = batch)
            outputs.append(output)
            node_feature, node_coord, batch = output[0], output[1], output[7]
        
        return outputs








@compile_mode('script')
class EdfExtractor(torch.nn.Module):  
    def __init__(self,
        irreps_inputs: List[o3.Irreps], 
        fc_neurons_inputs: List[List[int]],
        irreps_emb: o3.Irreps,
        irreps_edge_attr: o3.Irreps, 
        irreps_head: o3.Irreps,
        num_heads: int, 
        fc_neurons: List[int],
        n_layers: int,
        cutoffs: List[float],
        offsets: List[float],
        query_radius: float,
        irreps_mlp_mid: Union[o3.Irreps, int] = 3,
        attn_type: str = 'mlp',
        alpha_drop: float = 0.1,
        proj_drop: float = 0.1,
        drop_path_rate: float = 0.0):
        
        super().__init__()
        self.irreps_inputs: List[o3.Irreps] = [o3.Irreps(irreps) for irreps in irreps_inputs]
        self.fc_neurons_inputs: List[List[int]] = fc_neurons_inputs
        self.n_scales: int = len(self.irreps_inputs)
        self.cutoffs: List[float] = cutoffs
        self.offsets: List[float] = offsets
        assert len(self.offsets) == len(self.cutoffs) == len(self.fc_neurons_inputs) == self.n_scales

        self.irreps_emb: o3.Irreps = o3.Irreps(irreps_emb)
        self.emb_dim: int = self.irreps_emb.dim
        self.irreps_edge_attr: o3.Irreps = o3.Irreps(irreps_edge_attr)
        self.irreps_head: o3.Irreps = o3.Irreps(irreps_head)
        self.num_heads: int = num_heads
        self.fc_neurons: List[int] = fc_neurons
        self.n_layers: int = n_layers
        self.query_radius: float = query_radius
        assert self.n_layers >= 1

        self.pre_connect = torch.nn.ModuleList()
        self.pre_radial = torch.nn.ModuleList()
        self.pre_layers = torch.nn.ModuleList()
        for n in range(self.n_scales):
            self.pre_connect.append(
                RadiusConnect(r=self.cutoffs[n], offset=None, max_num_neighbors= 1000) # TODO: offset=None -> self.offsets[n]
            )
            fc = self.fc_neurons_inputs[n]
            self.pre_radial.append(
                GaussianRadialBasisLayerFiniteCutoff(num_basis=fc[0], 
                                                     cutoff=self.cutoffs[n], 
                                                     offset=self.offsets[n],
                                                     soft_cutoff=True)
            )
            self.pre_layers.append(
                EquiformerBlock(irreps_src = self.irreps_inputs[n], 
                                irreps_dst = self.irreps_emb, 
                                irreps_edge_attr = self.irreps_edge_attr, 
                                irreps_head = self.irreps_head,
                                num_heads = self.num_heads, 
                                fc_neurons = fc,
                                irreps_mlp_mid = irreps_mlp_mid,
                                attn_type = attn_type,
                                alpha_drop = alpha_drop,
                                proj_drop = proj_drop,
                                drop_path_rate = drop_path_rate,
                                src_bias = False,
                                dst_bias = True)
            )

        # self.post_connect = torch.nn.ModuleList()
        # self.post_radial = torch.nn.ModuleList()
        # self.post_layers = torch.nn.ModuleList()
        # for n in range(self.n_layers - 1):
        #     self.post_connect.append(
        #         RadiusGraph(r=self.query_radius, max_num_neighbors=1000)
        #     )
        #     self.post_radial.append(
        #         GaussianRadialBasisLayerFiniteCutoff(num_basis=self.fc_neurons[0], 
        #                                              cutoff=self.query_radius, 
        #                                              soft_cutoff=True)
        #     )
        #     self.post_layers.append(
        #         EquiformerBlock(irreps_src = self.irreps_emb, 
        #                         irreps_dst = self.irreps_emb, 
        #                         irreps_edge_attr = self.irreps_edge_attr, 
        #                         irreps_head = self.irreps_head,
        #                         num_heads = self.num_heads, 
        #                         fc_neurons = self.fc_neurons,
        #                         irreps_mlp_mid = irreps_mlp_mid,
        #                         attn_type = attn_type,
        #                         alpha_drop = alpha_drop,
        #                         proj_drop = proj_drop,
        #                         drop_path_rate = drop_path_rate,
        #                         src_bias = False,
        #                         dst_bias = True)
        #     )
        self.spherical_harmonics = o3.SphericalHarmonics(irreps_out = self.irreps_edge_attr, normalize = True, normalization='component')    
        self.register_buffer('zero_features', torch.zeros(1, self.emb_dim), persistent=False)
        self.proj = LinearRS(irreps_in = self.irreps_emb,
                             irreps_out = self.irreps_emb,
                             bias = True)


    def forward(self, query_coord: torch.Tensor,
                query_batch: torch.Tensor,
                node_features: List[torch.Tensor],
                node_coords: List[torch.Tensor],
                node_batches: List[torch.Tensor]) -> torch.Tensor:
        assert len(node_features) == len(node_coords) == len(node_batches) == self.n_scales
        assert query_coord.ndim == 2 and query_coord.shape[-1] == 3

        node_feature_dst = (self.zero_features.detach()).expand(len(query_coord), self.emb_dim)
        for n, (connect, radial, layers) in enumerate(zip(self.pre_connect, self.pre_radial, self.pre_layers)):
            edge_src, edge_dst = connect(node_coord_src = node_coords[n], 
                                         batch_src = node_batches[n],
                                         node_coord_dst = query_coord,
                                         batch_dst = query_batch)
            edge_vec = node_coords[n].index_select(0, edge_src) - query_coord.index_select(0, edge_dst)
            edge_attr = self.spherical_harmonics(edge_vec)
            edge_length = edge_vec.norm(dim=1, p=2)
            edge_scalar = radial(edge_length)

            node_feature_dst = node_feature_dst \
                               + layers(node_input_src = node_features[n],
                                        node_input_dst = (self.zero_features.detach()).expand(len(query_coord), self.emb_dim),
                                        batch_dst = query_batch,
                                        edge_src = edge_src,
                                        edge_dst = edge_dst,
                                        edge_attr = edge_attr,
                                        edge_scalars = edge_scalar)

        # for n in range(self.n_layers - 1):
        #     _, _, edge_src, edge_dst, degree, _ = self.post_connect[n](node_coord_src = query_coord, 
        #                                                                node_feature_src = node_feature_dst, 
        #                                                                batch_src = query_batch)
        #     edge_vec = query_coord.index_select(0, edge_src) - query_coord.index_select(0, edge_dst)
        #     edge_attr = self.spherical_harmonics(edge_vec)
        #     edge_length = edge_vec.norm(dim=1, p=2)
        #     edge_scalar = self.post_radial[n](edge_length)

        #     node_feature_dst = self.post_layers[n](node_input_src = node_feature_dst,
        #                                            node_input_dst = node_feature_dst,
        #                                            batch_dst = query_batch,
        #                                            edge_src = edge_src,
        #                                            edge_dst = edge_dst,
        #                                            edge_attr = edge_attr,
        #                                            edge_scalars = edge_scalar)
        
        return self.proj(node_feature_dst)
