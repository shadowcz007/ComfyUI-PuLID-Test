import torch
from torch import nn
import torchvision.transforms as T
import os
import folder_paths
import comfy.utils
from insightface.app import FaceAnalysis
import numpy as np
from copy import deepcopy
from codeformer.facelib.parsing.bisenet import BiSeNet
from codeformer.facelib.utils.face_restoration_helper import FaceRestoreHelper
from codeformer.facelib.detection.retinaface.retinaface import RetinaFace 

# 复用
dir_facedetection_models = os.path.join(folder_paths.models_dir, "facedetection")
def get_files_with_extension(directory, extension):
    file_list = []
    for root, dirs, files in os.walk(directory):
        for file in files:
            if file.endswith(extension):
                file = os.path.splitext(file)[0]
                file_path = os.path.join(root, file)
                file_name = os.path.relpath(file_path, directory)
                file_list.append(file_name)
    return file_list


class ModifiedFaceRestoreHelper(FaceRestoreHelper):
    # def __init__(self):
    #     # super().__init__()  # 调用父类的__init__方法
    #     print('modified init')
    def __init__(
        self,
        upscale_factor=1,
        face_size=512,
        crop_ratio=(1, 1),
        dirpath="",
        use_parse=False,
        device=None,
    ):
        model_name="detection_mobilenet0.25_Final"

        self.template_3points = False  # improve robustness
        self.upscale_factor = int(upscale_factor)
        # the cropped face ratio based on the square face
        self.crop_ratio = crop_ratio  # (h, w)
        assert self.crop_ratio[0] >= 1 and self.crop_ratio[1] >= 1, "crop ration only supports >=1"
        self.face_size = (int(face_size * self.crop_ratio[1]), int(face_size * self.crop_ratio[0]))
        self.model_name = model_name
        self.det_model = model_name
        self.pad_blur=False

        # standard 5 landmarks for FFHQ faces with 512 x 512
        self.face_template = np.array(
                [
                    [192.98138, 239.94708],
                    [318.90277, 240.1936],
                    [256.63416, 314.01935],
                    [201.26117, 371.41043],
                    [313.08905, 371.15118],
                ]
            )
 
        self.face_template = self.face_template * (face_size / 512.0)

        self.save_ext = "png"
     
        self.all_landmarks_5 = []
        self.det_faces = []
        self.affine_matrices = []
        self.inverse_affine_matrices = []
        self.cropped_faces = []
        self.restored_faces = []
        self.pad_input_imgs = []

        if device is None:
            self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        else:
            self.device = device

        # init face detection model
        def init_retinaface_model(dir_path,model_name, half=False, device='cuda'):

            model = RetinaFace(network_name='mobile0.25', half=half)
            model_path=os.path.join(dir_path,"detection_mobilenet0.25_Final.pth")
          
            load_net = torch.load(model_path, map_location=lambda storage, loc: storage)
            # remove unnecessary 'module.'
            for k, v in deepcopy(load_net).items():
                if k.startswith('module.'):
                    load_net[k[7:]] = v
                    load_net.pop(k)
            model.load_state_dict(load_net, strict=True)
            model.eval()
            model = model.to(device)

            return model

        
        self.face_detector = init_retinaface_model(dirpath,model_name, half=False, device=self.device)
                
        def init_parsing_model(dir_path,model_name='bisenet', half=False, device='cuda'):
            if model_name == 'bisenet':
                model = BiSeNet(num_class=19)
                model_path=os.path.join(dir_path,'parsing_bisenet.pth')
                model_url = 'https://github.com/sczhou/CodeFormer/releases/download/v0.1.0/parsing_bisenet.pth'
            elif model_name == 'parsenet':
                model = ParseNet(in_size=512, out_size=512, parsing_ch=19)
                model_path=os.path.join(dir_path,'parsing_parsenet.pth')
                model_url = 'https://github.com/sczhou/CodeFormer/releases/download/v0.1.0/parsing_parsenet.pth'
            else:
                raise NotImplementedError(f'{model_name} is not implemented.')

            # model_path = load_file_from_url(url=model_url, model_dir='weights/facelib', progress=True, file_name=None)
            load_net = torch.load(model_path, map_location=lambda storage, loc: storage)
            model.load_state_dict(load_net, strict=True)
            model.eval()
            model = model.to(device)
            return model

        # init face parsing model
        self.use_parse = use_parse
        self.face_parse = init_parsing_model(dirpath,model_name='bisenet', device=self.device)



from comfy.ldm.modules.attention import optimized_attention

from .eva_clip.constants import OPENAI_DATASET_MEAN, OPENAI_DATASET_STD

from .encoders import IDEncoder

INSIGHTFACE_DIR = os.path.join(folder_paths.models_dir, "insightface")

MODELS_DIR = os.path.join(folder_paths.models_dir, "pulid")
if "pulid" not in folder_paths.folder_names_and_paths:
    current_paths = [MODELS_DIR]
else:
    current_paths, _ = folder_paths.folder_names_and_paths["pulid"]
folder_paths.folder_names_and_paths["pulid"] = (current_paths, folder_paths.supported_pt_extensions)

class PulidModel(nn.Module):
    def __init__(self, model):
        super().__init__()

        self.model = model
        self.image_proj_model = self.init_id_adapter()
        self.image_proj_model.load_state_dict(model["image_proj"])
        self.ip_layers = To_KV(model["ip_adapter"])
    
    def init_id_adapter(self):
        image_proj_model = IDEncoder()
        return image_proj_model

    def get_image_embeds(self, face_embed, clip_embeds):
        embeds = self.image_proj_model(face_embed, clip_embeds)
        return embeds

class To_KV(nn.Module):
    def __init__(self, state_dict):
        super().__init__()

        self.to_kvs = nn.ModuleDict()
        for key, value in state_dict.items():
            self.to_kvs[key.replace(".weight", "").replace(".", "_")] = nn.Linear(value.shape[1], value.shape[0], bias=False)
            self.to_kvs[key.replace(".weight", "").replace(".", "_")].weight.data = value

def tensor_to_image(tensor):
    image = tensor.mul(255).clamp(0, 255).byte().cpu()
    image = image[..., [2, 1, 0]].numpy()
    return image

def image_to_tensor(image):
    tensor = torch.clamp(torch.from_numpy(image).float() / 255., 0, 1)
    tensor = tensor[..., [2, 1, 0]]
    return tensor

def tensor_to_size(source, dest_size):
    if isinstance(dest_size, torch.Tensor):
        dest_size = dest_size.shape[0]
    source_size = source.shape[0]

    if source_size < dest_size:
        shape = [dest_size - source_size] + [1]*(source.dim()-1)
        source = torch.cat((source, source[-1:].repeat(shape)), dim=0)
    elif source_size > dest_size:
        source = source[:dest_size]

def set_model_patch_replace(model, patch_kwargs, key):
    to = model.model_options["transformer_options"].copy()
    if "patches_replace" not in to:
        to["patches_replace"] = {}
    else:
        to["patches_replace"] = to["patches_replace"].copy()

    if "attn2" not in to["patches_replace"]:
        to["patches_replace"]["attn2"] = {}
    else:
        to["patches_replace"]["attn2"] = to["patches_replace"]["attn2"].copy()
    
    if key not in to["patches_replace"]["attn2"]:
        to["patches_replace"]["attn2"][key] = Attn2Replace(pulid_attention, **patch_kwargs)
        model.model_options["transformer_options"] = to
    else:
        to["patches_replace"]["attn2"][key].add(pulid_attention, **patch_kwargs)

class Attn2Replace:
    def __init__(self, callback=None, **kwargs):
        self.callback = [callback]
        self.kwargs = [kwargs]
    
    def add(self, callback, **kwargs):          
        self.callback.append(callback)
        self.kwargs.append(kwargs)

        for key, value in kwargs.items():
            setattr(self, key, value)

    def __call__(self, q, k, v, extra_options):
        dtype = q.dtype
        out = optimized_attention(q, k, v, extra_options["n_heads"])
        sigma = extra_options["sigmas"].detach().cpu()[0].item() if 'sigmas' in extra_options else 999999999.9

        for i, callback in enumerate(self.callback):
            if sigma <= self.kwargs[i]["sigma_start"] and sigma >= self.kwargs[i]["sigma_end"]:
                out = out + callback(out, q, k, v, extra_options, **self.kwargs[i])
        
        return out.to(dtype=dtype)

def pulid_attention(out, q, k, v, extra_options, module_key='', pulid=None, cond=None, uncond=None, weight=1.0, num_zero=8, ortho=False, ortho_v2=False, **kwargs):
    k_key = module_key + "_to_k_ip"
    v_key = module_key + "_to_v_ip"

    dtype = q.dtype
    cond_or_uncond = extra_options["cond_or_uncond"]
    b = q.shape[0]
    batch_prompt = b // len(cond_or_uncond)

    #conds = torch.cat([uncond.repeat(batch_prompt, 1, 1), cond.repeat(batch_prompt, 1, 1)], dim=0)
    #zero_tensor = torch.zeros((conds.size(0), num_zero, conds.size(-1)), dtype=conds.dtype, device=conds.device)
    #conds = torch.cat([conds, zero_tensor], dim=1)
    #ip_k = pulid.ip_layers.to_kvs[k_key](conds)
    #ip_v = pulid.ip_layers.to_kvs[v_key](conds)
    
    if num_zero > 0:
        zero_tensor = torch.zeros((cond.size(0), num_zero, cond.size(-1)), dtype=cond.dtype, device=cond.device)
        cond = torch.cat([cond, zero_tensor], dim=1)
        uncond = torch.cat([uncond, zero_tensor], dim=1)
    k_cond = pulid.ip_layers.to_kvs[k_key](cond).repeat(batch_prompt, 1, 1)
    k_uncond = pulid.ip_layers.to_kvs[k_key](uncond).repeat(batch_prompt, 1, 1)
    v_cond = pulid.ip_layers.to_kvs[v_key](cond).repeat(batch_prompt, 1, 1)
    v_uncond = pulid.ip_layers.to_kvs[v_key](uncond).repeat(batch_prompt, 1, 1)
    ip_k = torch.cat([(k_cond, k_uncond)[i] for i in cond_or_uncond], dim=0)
    ip_v = torch.cat([(v_cond, v_uncond)[i] for i in cond_or_uncond], dim=0)

    out_ip = optimized_attention(q, ip_k, ip_v, extra_options["n_heads"])
    
    if ortho:
        out = out.to(dtype=torch.float32)
        out_ip = out_ip.to(dtype=torch.float32)
        projection = (torch.sum((out * out_ip), dim=-2, keepdim=True) / torch.sum((out * out), dim=-2, keepdim=True) * out)
        orthogonal = out_ip - projection
        out = weight * orthogonal
    elif ortho_v2:
        out = out.to(dtype=torch.float32)
        out_ip = out_ip.to(dtype=torch.float32)
        attn_map = q @ ip_k.transpose(-2, -1)
        attn_mean = attn_map.softmax(dim=-1).mean(dim=1, keepdim=True)
        attn_mean = attn_mean[:, :, :5].sum(dim=-1, keepdim=True)
        projection = (torch.sum((out * out_ip), dim=-2, keepdim=True) / torch.sum((out * out), dim=-2, keepdim=True) * out)
        orthogonal = out_ip + (attn_mean - 1) * projection
        out = weight * orthogonal
    else:
        out = out_ip * weight

    return out.to(dtype=dtype)

def to_gray(img):
    x = 0.299 * img[:, 0:1] + 0.587 * img[:, 1:2] + 0.114 * img[:, 2:3]
    x = x.repeat(1, 3, 1, 1)
    return x

"""
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
 Nodes
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""

class PulidModelLoader:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": { "pulid_file": (folder_paths.get_filename_list("pulid"), )}}

    RETURN_TYPES = ("PULID",)
    FUNCTION = "load_model"
    CATEGORY = "pulid"

    def load_model(self, pulid_file):
        ckpt_path = folder_paths.get_full_path("pulid", pulid_file)

        model = comfy.utils.load_torch_file(ckpt_path, safe_load=True)

        if ckpt_path.lower().endswith(".safetensors"):
            st_model = {"image_proj": {}, "ip_adapter": {}}
            for key in model.keys():
                if key.startswith("image_proj."):
                    st_model["image_proj"][key.replace("image_proj.", "")] = model[key]
                elif key.startswith("ip_adapter."):
                    st_model["ip_adapter"][key.replace("ip_adapter.", "")] = model[key]
            model = st_model

        return (model,)

class PulidInsightFaceLoader:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "provider": (["CPU", "CUDA", "ROCM"], ),
            },
        }

    RETURN_TYPES = ("FACEANALYSIS",)
    FUNCTION = "load_insightface"
    CATEGORY = "pulid"

    def load_insightface(self, provider):
        model = FaceAnalysis(name="antelopev2", root=INSIGHTFACE_DIR, providers=[provider + 'ExecutionProvider',]) # alternative to buffalo_l
        model.prepare(ctx_id=0, det_size=(640, 640))

        return (model,)

class PulidEvaClipLoader:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {},
        }

    RETURN_TYPES = ("EVA_CLIP",)
    FUNCTION = "load_eva_clip"
    CATEGORY = "pulid"

    def load_eva_clip(self):
        from .eva_clip.factory import create_model

        device="cuda" if torch.cuda.is_available() else "cpu"

        model = create_model(
            'EVA02-CLIP-L-14-336', 
            'eva_clip', 
            precision='fp32' if device == 'cpu' else 'fp16',
            device=device,
            jit=False,
            force_custom_clip=True, 
            cache_dir=MODELS_DIR, 
        )

        model = model.visual

        eva_transform_mean = getattr(model, 'image_mean', OPENAI_DATASET_MEAN)
        eva_transform_std = getattr(model, 'image_std', OPENAI_DATASET_STD)
        if not isinstance(eva_transform_mean, (list, tuple)):
            model["image_mean"] = (eva_transform_mean,) * 3
        if not isinstance(eva_transform_std, (list, tuple)):
            model["image_std"] = (eva_transform_std,) * 3

        return (model,)


class ApplyPulid:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "model": ("MODEL", ),
                "pulid": ("PULID", ),
                "eva_clip": ("EVA_CLIP", ),
                "face_analysis": ("FACEANALYSIS", ),
                "image": ("IMAGE", ),
                "method": (["fidelity", "style", "neutral"],),
                "weight": ("FLOAT", {"default": 1.0, "min": -1.0, "max": 5.0, "step": 0.05 }),
                "start_at": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 1.0, "step": 0.001 }),
                "end_at": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0, "step": 0.001 }),
                # "facedetection": (get_files_with_extension(dir_facedetection_models,'.pth'),),
            },
        }

    RETURN_TYPES = ("MODEL",)
    FUNCTION = "apply_pulid"
    CATEGORY = "pulid"

    def apply_pulid(self, model, pulid, eva_clip, face_analysis, image, method, weight, start_at, end_at):
        work_model = model.clone()
        
        device = comfy.model_management.get_torch_device()
        dtype = comfy.model_management.unet_dtype()
        if dtype not in [torch.float32, torch.float16, torch.bfloat16]:
            dtype = torch.float16 if comfy.model_management.should_use_fp16() else torch.float32

        eva_clip.to(device, dtype=dtype)
        pulid_model = PulidModel(pulid).to(device, dtype=dtype)

        if method == "fidelity":
            num_zero = 8
            ortho = False
            ortho_v2 = True
        elif method == "style":
            num_zero = 16
            ortho = True
            ortho_v2 = False
        else:
            num_zero = 0
            ortho = False
            ortho_v2 = False

        #face_analysis.det_model.input_size = (640,640)
        image = tensor_to_image(image)

        face_helper = ModifiedFaceRestoreHelper( 
            dirpath=dir_facedetection_models,
            device=device,
        )

 
        bg_label = [0, 16, 18, 7, 8, 9, 14, 15]
        cond = []
        uncond = []

        for i in range(image.shape[0]):
            # get insightface embeddings
            iface_embeds = None
            for size in [(size, size) for size in range(640, 256, -64)]:
                face_analysis.det_model.input_size = size
                face = face_analysis.get(image[i])
                if face:
                    face = sorted(face, key=lambda x: (x.bbox[2] - x.bbox[0]) * (x.bbox[3] - x.bbox[1]), reverse=True)[-1]
                    iface_embeds = torch.from_numpy(face.embedding).unsqueeze(0).to(device, dtype=dtype)
                    break
            else:
                raise Exception('insightface: No face detected.')

            # get eva_clip embeddings
            face_helper.clean_all()
            face_helper.read_image(image[i])
            face_helper.get_face_landmarks_5(only_center_face=True)
            face_helper.align_warp_face()

            if len(face_helper.cropped_faces) == 0:
                raise Exception('facexlib: No face detected.')
            
            face = face_helper.cropped_faces[0]
            face = image_to_tensor(face).unsqueeze(0).permute(0,3,1,2).to(device)


            parsing_out = face_helper.face_parse(T.functional.normalize(face, [0.485, 0.456, 0.406], [0.229, 0.224, 0.225]))[0]
            parsing_out = parsing_out.argmax(dim=1, keepdim=True)
            bg = sum(parsing_out == i for i in bg_label).bool()
            white_image = torch.ones_like(face)
            face_features_image = torch.where(bg, white_image, to_gray(face))
            face_features_image = T.functional.resize(face_features_image, eva_clip.image_size, T.InterpolationMode.BICUBIC).to(device, dtype=dtype)
            face_features_image = T.functional.normalize(face_features_image, eva_clip.image_mean, eva_clip.image_std)
            

            id_cond_vit, id_vit_hidden = eva_clip(face_features_image, return_all_features=False, return_hidden=True, shuffle=False)
            id_cond_vit = id_cond_vit.to(device, dtype=dtype)
            for idx in range(len(id_vit_hidden)):
                id_vit_hidden[idx] = id_vit_hidden[idx].to(device, dtype=dtype)

            id_cond_vit = torch.div(id_cond_vit, torch.norm(id_cond_vit, 2, 1, True))

            # combine embeddings
            id_cond = torch.cat([iface_embeds, id_cond_vit], dim=-1)
            id_uncond = torch.zeros_like(id_cond)
            id_vit_hidden_uncond = []
            for idx in range(len(id_vit_hidden)):
                id_vit_hidden_uncond.append(torch.zeros_like(id_vit_hidden[idx]))
            
            cond.append(pulid_model.get_image_embeds(id_cond, id_vit_hidden))
            uncond.append(pulid_model.get_image_embeds(id_uncond, id_vit_hidden_uncond))
        
        # average embeddings
        cond = torch.cat(cond).to(device, dtype=dtype)
        uncond = torch.cat(uncond).to(device, dtype=dtype)
        if cond.shape[0] > 1:
            cond = torch.mean(cond, dim=0, keepdim=True)
            uncond = torch.mean(uncond, dim=0, keepdim=True)

        sigma_start = work_model.get_model_object("model_sampling").percent_to_sigma(start_at)
        sigma_end = work_model.get_model_object("model_sampling").percent_to_sigma(end_at)

        patch_kwargs = {
            "pulid": pulid_model,
            "weight": weight,
            "cond": cond,
            "uncond": uncond,
            "sigma_start": sigma_start,
            "sigma_end": sigma_end,
            "num_zero": num_zero,
            "ortho": ortho,
            "ortho_v2": ortho_v2,
        }

        number = 0
        for id in [4,5,7,8]: # id of input_blocks that have cross attention
            block_indices = range(2) if id in [4, 5] else range(10) # transformer_depth
            for index in block_indices:
                patch_kwargs["module_key"] = str(number*2+1)
                set_model_patch_replace(work_model, patch_kwargs, ("input", id, index))
                number += 1
        for id in range(6): # id of output_blocks that have cross attention
            block_indices = range(2) if id in [3, 4, 5] else range(10) # transformer_depth
            for index in block_indices:
                patch_kwargs["module_key"] = str(number*2+1)
                set_model_patch_replace(work_model, patch_kwargs, ("output", id, index))
                number += 1
        for index in range(10):
            patch_kwargs["module_key"] = str(number*2+1)
            set_model_patch_replace(work_model, patch_kwargs, ("middle", 0, index))
            number += 1

        return (work_model,)

NODE_CLASS_MAPPINGS = {
    "PulidModelLoader": PulidModelLoader,
    "PulidInsightFaceLoader": PulidInsightFaceLoader,
    "PulidEvaClipLoader": PulidEvaClipLoader,
    "ApplyPulid": ApplyPulid,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "PulidModelLoader": "Load Pulid Model",
    "PulidInsightFaceLoader": "Load InsightFace",
    "PulidEvaClipLoader": "Load Eva Clip",
    "ApplyPulid": "Apply Pulid",
}