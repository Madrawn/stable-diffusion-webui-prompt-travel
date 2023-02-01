import os
from enum import Enum
from pathlib import Path
from copy import deepcopy
from PIL.Image import Image as PILImage
from typing import List, Tuple, Union, Callable
from traceback import print_exc

import gradio as gr
import numpy as np
from torch import Tensor
import torch.nn.functional as F
try:
    from moviepy.video.io.ImageSequenceClip import ImageSequenceClip
    from moviepy.editor import concatenate_videoclips, ImageClip
except ImportError:
    print('package moviepy not installed, will not be able to generate video')

from modules.scripts import Script
from modules.script_callbacks import on_before_image_saved, remove_callbacks_for_function, ImageSaveParams
from modules.ui import gr_show
from modules.shared import state, opts, sd_upscalers
from modules.prompt_parser import ScheduledPromptConditioning, MulticondLearnedConditioning
from modules.processing import Processed, StableDiffusionProcessing, StableDiffusionProcessingTxt2Img, StableDiffusionProcessingImg2Img, get_fixed_seed
from modules.images import resize_image
from modules.sd_samplers_common import single_sample_to_image

class Mode(Enum):
    LINEAR  = 'linear'
    REPLACE = 'replace'

class LerpMethod(Enum):
    LERP  = 'lerp'
    SLERP = 'slerp'

class ModeReplaceDim(Enum):
    TOKEN   = 'token'
    CHANNEL = 'channel'
    RANDOM  = 'random'

class ModeReplaceOrder(Enum):
    SIMILAR   = 'similar'
    DIFFERENT = 'different'
    RANDOM    = 'random'

class Gensis(Enum):
    FIXED      = 'fixed'
    SUCCESSIVE = 'successive'
    EMBRYO     = 'embryo'

class VideoFormat(Enum):
    MP4  = 'mp4'
    GIF  = 'gif'
    WEBM = 'webm'

if 'global consts':
    __ = lambda key, value=None: opts.data.get(f'customscript/prompt_travel.py/txt2img/{key}/value', value)

    LABEL_MODE              = 'Travel mode'
    LABEL_STEPS             = 'Travel steps between stages'
    LABEL_GENESIS           = 'Frame genesis'
    LABEL_DENOISE_W         = 'Denoise strength'
    LABEL_EMBRYO_STEP       = 'Denoise steps for embryo'
    LABEL_LERP_METH         = 'Linear interp method'
    LABEL_REPLACE_DIM       = 'Replace dimension'
    LABEL_REPLACE_ORDER     = 'Replace order'
    LABEL_UPSCALE_METH      = 'Upscaler'
    LABEL_UPSCALE_RATIO     = 'Upscale ratio'
    LABEL_VIDEO_FPS         = 'Video FPS'
    LABEL_VIDEO_FMT         = 'Video file format'
    LABEL_VIDEO_PAD         = 'Pad begin/end frames'
    LABEL_VIDEO_PICK        = 'Pick frame by slice'
    LABEL_DEBUG             = 'Show console debug'

    DEFAULT_MODE            = __(LABEL_MODE, Mode.LINEAR.value)
    DEFAULT_STEPS           = __(LABEL_STEPS, 30)
    DEFAULT_GENESIS         = __(LABEL_GENESIS, Gensis.FIXED.value)
    DEFAULT_DENOISE_W       = __(LABEL_DENOISE_W, 1.0)
    DEFAULT_EMBRYO_STEP     = __(LABEL_EMBRYO_STEP, 8)
    DEFAULT_LERP_METH       = __(LABEL_LERP_METH, LerpMethod.LERP.value)
    DEFAULT_REPLACE_DIM     = __(LABEL_REPLACE_DIM, ModeReplaceDim.TOKEN.value)
    DEFAULT_REPLACE_ORDER   = __(LABEL_REPLACE_ORDER, ModeReplaceOrder.RANDOM.value)
    DEFAULT_UPSCALE_METH    = __(LABEL_UPSCALE_METH, 'Lanczos')
    DEFAULT_UPSCALE_RATIO   = __(LABEL_UPSCALE_RATIO, 1.0)
    DEFAULT_VIDEO_FPS       = __(LABEL_VIDEO_FPS, 10)
    DEFAULT_VIDEO_FMT       = __(LABEL_VIDEO_FMT, VideoFormat.MP4.value)
    DEFAULT_VIDEO_PAD       = __(LABEL_VIDEO_PAD, 0)
    DEFAULT_VIDEO_PICK      = __(LABEL_VIDEO_PICK, '')
    DEFAULT_DEBUG           = __(LABEL_DEBUG, True)

    CHOICES_MODE            = [x.value for x in Mode]
    CHOICES_LERP_METH       = [x.value for x in LerpMethod]
    CHOICES_GENESIS         = [x.value for x in Gensis]
    CHOICES_REPLACE_DIM     = [x.value for x in ModeReplaceDim]
    CHOICES_REPLACE_ORDER   = [x.value for x in ModeReplaceOrder]
    CHOICES_UPSCALER        = [x.name for x in sd_upscalers]
    CHOICES_VIDEO_FMT       = [x.value for x in VideoFormat]

    EPS = 1e-6


# ↓↓↓ the following is modified from 'modules/processing.py' ↓↓↓

from modules.processing import *

extra_network_data = None

def process_images_before(p: StableDiffusionProcessing):
    try:
        for k, v in p.override_settings.items():
            setattr(opts, k, v)

            if k == 'sd_model_checkpoint':
                sd_models.reload_model_weights()

            if k == 'sd_vae':
                sd_vae.reload_vae_weights()
    except:
        pass

def process_images_after(p: StableDiffusionProcessing):
    stored_opts = {k: opts.data[k] for k in p.override_settings.keys()}

    # restore opts to original state
    if p.override_settings_restore_afterwards:
        for k, v in stored_opts.items():
            setattr(opts, k, v)
            if k == 'sd_model_checkpoint':
                sd_models.reload_model_weights()

            if k == 'sd_vae':
                sd_vae.reload_vae_weights()

def process_images_prompt_to_cond(p: StableDiffusionProcessing) -> tuple:
    """this is the main loop that both txt2img and img2img use; it calls func_init once inside all the scopes and func_sample once per batch"""
    
    global extra_network_data

    if type(p.prompt) == list:
        assert(len(p.prompt) > 0)
    else:
        assert p.prompt is not None

    devices.torch_gc()

    seed    = p.seed
    subseed = p.subseed

    modules.sd_hijack.model_hijack.apply_circular(p.tiling)
    modules.sd_hijack.model_hijack.clear_comments()

    if type(p.prompt) == list:
        p.all_prompts = [shared.prompt_styles.apply_styles_to_prompt(x, p.styles) for x in p.prompt]
    else:
        p.all_prompts = p.batch_size * p.n_iter * [shared.prompt_styles.apply_styles_to_prompt(p.prompt, p.styles)]

    if type(p.negative_prompt) == list:
        p.all_negative_prompts = [shared.prompt_styles.apply_negative_styles_to_prompt(x, p.styles) for x in p.negative_prompt]
    else:
        p.all_negative_prompts = p.batch_size * p.n_iter * [shared.prompt_styles.apply_negative_styles_to_prompt(p.negative_prompt, p.styles)]

    if type(seed) == list:
        p.all_seeds = seed
    else:
        p.all_seeds = [int(seed) + (x if p.subseed_strength == 0 else 0) for x in range(len(p.all_prompts))]

    if type(subseed) == list:
        p.all_subseeds = subseed
    else:
        p.all_subseeds = [int(subseed) + x for x in range(len(p.all_prompts))]

    if os.path.exists(cmd_opts.embeddings_dir) and not p.do_not_reload_embeddings:
        model_hijack.embedding_db.load_textual_inversion_embeddings()

    _, extra_network_data = extra_networks.parse_prompts(p.all_prompts[0:1])

    if p.scripts is not None:
        p.scripts.process(p)

    cached_uc = [None, None]
    cached_c = [None, None]

    def get_conds_with_caching(function, required_prompts, steps, cache):
        """
        Returns the result of calling function(shared.sd_model, required_prompts, steps)
        using a cache to store the result if the same arguments have been used before.

        cache is an array containing two elements. The first element is a tuple
        representing the previously used arguments, or None if no arguments
        have been used before. The second element is where the previously
        computed result is stored.
        """

        if cache[0] is not None and (required_prompts, steps) == cache[0]:
            return cache[1]

        with devices.autocast():
            cache[1] = function(shared.sd_model, required_prompts, steps)

        cache[0] = (required_prompts, steps)
        return cache[1]

    with torch.no_grad(), p.sd_model.ema_scope():
        with devices.autocast():
            p.init(p.all_prompts, p.all_seeds, p.all_subseeds)

            # for OSX, loading the model during sampling changes the generated picture, so it is loaded here
            if shared.opts.live_previews_enable and opts.show_progress_type == "Approx NN":
                sd_vae_approx.model()

            if not p.disable_extra_networks:
                extra_networks.activate(p, extra_network_data)

        with open(os.path.join(shared.script_path, "params.txt"), "w", encoding="utf8") as file:
            processed = Processed(p, [], p.seed, "")
            file.write(processed.infotext(p, 0))

        if state.job_count == -1:
            state.job_count = p.n_iter

        if state.skipped:
            state.skipped = False
        
        if state.interrupted:
            return

        n = 0
        prompts = p.all_prompts[n * p.batch_size:(n + 1) * p.batch_size]
        negative_prompts = p.all_negative_prompts[n * p.batch_size:(n + 1) * p.batch_size]
        seeds = p.all_seeds[n * p.batch_size:(n + 1) * p.batch_size]
        subseeds = p.all_subseeds[n * p.batch_size:(n + 1) * p.batch_size]

        if len(prompts) == 0:
            return

        prompts, _ = extra_networks.parse_prompts(prompts)

        if p.scripts is not None:
            p.scripts.process_batch(p, batch_number=n, prompts=prompts, seeds=seeds, subseeds=subseeds)

        with devices.autocast():
            # 'prompt string' => tensor([T, D])
            uc = get_conds_with_caching(prompt_parser.get_learned_conditioning, negative_prompts, p.steps, cached_uc)
            c = get_conds_with_caching(prompt_parser.get_multicond_learned_conditioning, prompts, p.steps, cached_c)

        devices.torch_gc()

        return c, uc, prompts, seeds, subseeds

def process_images_cond_to_image(p: StableDiffusionProcessing, c, uc, prompts, seeds, subseeds) -> Processed:
    global extra_network_data

    comments = {}
    infotexts = []
    output_images = []

    def infotext(iteration=0, position_in_batch=0):
        return create_infotext(p, p.all_prompts, p.all_seeds, p.all_subseeds, comments, iteration, position_in_batch)

    if len(model_hijack.comments) > 0:
        for comment in model_hijack.comments:
            comments[comment] = 1
    
    with torch.no_grad(), p.sd_model.ema_scope():
        
        with devices.without_autocast() if devices.unet_needs_upcast else devices.autocast():
            samples_ddim = p.sample(conditioning=c, unconditional_conditioning=uc, seeds=seeds, subseeds=subseeds, subseed_strength=p.subseed_strength, prompts=prompts)

        # [B=1, C=4, H=64,  W=64] => [B=1, C=3, H=512, W=512]
        x_samples_ddim = [decode_first_stage(p.sd_model, samples_ddim[i:i+1].to(dtype=devices.dtype_vae))[0].cpu() for i in range(samples_ddim.size(0))]
        for x in x_samples_ddim: devices.test_for_nans(x, "vae")
        x_samples_ddim = torch.stack(x_samples_ddim).float()
        x_samples_ddim = torch.clamp((x_samples_ddim + 1.0) / 2.0, min=0.0, max=1.0)

        del samples_ddim

        if shared.cmd_opts.lowvram or shared.cmd_opts.medvram:
            lowvram.send_everything_to_cpu()
        
        devices.torch_gc()

        n = 0       # batch count for legacy compatible

        if p.scripts is not None:
            p.scripts.postprocess_batch(p, x_samples_ddim, batch_number=n)

        for i, x_sample in enumerate(x_samples_ddim):
            x_sample = 255. * np.moveaxis(x_sample.cpu().numpy(), 0, 2)
            x_sample = x_sample.astype(np.uint8)

            if p.restore_faces:
                if opts.save and not p.do_not_save_samples and opts.save_images_before_face_restoration:
                    images.save_image(Image.fromarray(x_sample), p.outpath_samples, "", seeds[i], prompts[i], opts.samples_format, info=infotext(n, i), p=p, suffix="-before-face-restoration")

                devices.torch_gc()

                x_sample = modules.face_restoration.restore_faces(x_sample)
                devices.torch_gc()

            image = Image.fromarray(x_sample)

            if p.scripts is not None:
                pp = scripts.PostprocessImageArgs(image)
                p.scripts.postprocess_image(p, pp)
                image = pp.image

            if p.color_corrections is not None and i < len(p.color_corrections):
                if opts.save and not p.do_not_save_samples and opts.save_images_before_color_correction:
                    image_without_cc = apply_overlay(image, p.paste_to, i, p.overlay_images)
                    images.save_image(image_without_cc, p.outpath_samples, "", seeds[i], prompts[i], opts.samples_format, info=infotext(n, i), p=p, suffix="-before-color-correction")
                image = apply_color_correction(p.color_corrections[i], image)

            image = apply_overlay(image, p.paste_to, i, p.overlay_images)

            if opts.samples_save and not p.do_not_save_samples:
                images.save_image(image, p.outpath_samples, "", seeds[i], prompts[i], opts.samples_format, info=infotext(n, i), p=p)

            text = infotext(n, i)
            infotexts.append(text)
            if opts.enable_pnginfo:
                image.info["parameters"] = text
            output_images.append(image)

        del x_samples_ddim 

        devices.torch_gc()

        state.nextjob()

    p.color_corrections = None
    
    index_of_first_image = 0

    if not p.disable_extra_networks:
        extra_networks.deactivate(p, extra_network_data)

    devices.torch_gc()

    res = Processed(p, output_images, p.all_seeds[0], infotext(), comments="".join(["\n\n" + x for x in comments]), subseed=p.all_subseeds[0], index_of_first_image=index_of_first_image, infotexts=infotexts)

    if p.scripts is not None:
        p.scripts.postprocess(p, res)
    
    return res

# ↑↑↑ the above is modified from 'modules/processing.py' ↑↑↑


Conditioning = Union[ScheduledPromptConditioning, MulticondLearnedConditioning]

def cond_get(X:Conditioning) -> Tensor:
    def spc_get_cond(c:List[List[ScheduledPromptConditioning]]) -> Tensor:
        return c[0][0].cond
    
    def mlc_get_cond(c:MulticondLearnedConditioning) -> Tensor:
        return c.batch[0][0].schedules[0].cond      # [B=1, T=77, D=768]

    return mlc_get_cond(X) if isinstance(X, MulticondLearnedConditioning) else spc_get_cond(X)

def cond_replace(X:Conditioning, condX:Tensor) -> Conditioning:
    def spc_replace_cond(c:List[List[ScheduledPromptConditioning]], cond: Tensor) -> ScheduledPromptConditioning:
        r = deepcopy(c)
        spc = r[0][0]
        r[0][0] = ScheduledPromptConditioning(spc.end_at_step, cond=cond)
        return r

    def mlc_replace_cond(c:MulticondLearnedConditioning, cond: Tensor) -> MulticondLearnedConditioning:
        r = deepcopy(c)
        spc = r.batch[0][0].schedules[0]
        r.batch[0][0].schedules[0] = ScheduledPromptConditioning(spc.end_at_step, cond=cond)
        return r

    return mlc_replace_cond(X, condX) if isinstance(X, MulticondLearnedConditioning) else spc_replace_cond(X, condX)

def cond_align(condA:Tensor, condB:Tensor) -> Tuple[Tensor, Tensor]:
    d = condA.shape[0] - condB.shape[0]
    if   d < 0: condA = F.pad(condA, (0, 0, 0, -d))
    elif d > 0: condB = F.pad(condB, (0, 0, 0,  d))
    return condA, condB
    
def wrap_get_align_replace(fn:Callable[..., Tensor]):
    def wrapper(A:Conditioning, B:Conditioning, *args, **kwargs) -> Conditioning:
        condA = cond_get(A)
        condB = cond_get(B)
        condA, condB = cond_align(condA, condB)
        condC = fn(condA, condB, *args, **kwargs)
        C = cond_replace(A, condC)
        return C
    return wrapper

@wrap_get_align_replace
def weighted_sum(condA:Tensor, condB:Tensor, alpha:float) -> Tensor:
    ''' linear interpolate on latent space of condition '''

    return (1 - alpha) * condA + (alpha) * condB

@wrap_get_align_replace
def geometric_slerp(condA:Tensor, condB:Tensor, alpha:float) -> Tensor:
    ''' spherical linear interpolation on latent space of condition, ref: https://en.wikipedia.org/wiki/Slerp '''

    A_n = condA / torch.norm(condA, dim=-1, keepdim=True)   # [T=77, D=768]
    B_n = condB / torch.norm(condB, dim=-1, keepdim=True)

    dot = (A_n * B_n).sum(dim=-1, keepdim=True)     # [T=77, D=1]
    omega = torch.acos(dot)                         # [T=77, D=1]
    so = torch.sin(omega)                           # [T=77, D=1]

    slerp = (torch.sin((1 - alpha) * omega) / so) * condA + (torch.sin(alpha * omega) / so) * condB

    mask = dot > 0.9995                             # [T=77, D=1]
    if not any(mask):
        return slerp
    else:
        lerp = (1 - alpha) * condA + (alpha) * condB
        return torch.where(mask, lerp, slerp)           # use simple lerp when angle very close to avoid NaN

@wrap_get_align_replace
def replace_until_match(condA:Tensor, condB:Tensor, count:int, dist:Tensor, order:str=ModeReplaceOrder.RANDOM) -> Tensor:
    ''' value substite on condition tensor; will inplace modify `dist` '''

    def index_tensor_to_tuple(index:Tensor) -> Tuple[Tensor, ...]:
        return tuple([index[..., i] for i in range(index.shape[-1])])       # tuple([nDiff], ...)

    # mask: [T=77, D=768], [T=77] or [D=768]
    mask = dist > EPS
    # idx_diff: [nDiff, nDim=2] or [nDiff, nDim=1]
    idx_diff = torch.nonzero(mask)
    n_diff = len(idx_diff)

    if order == ModeReplaceOrder.RANDOM:
        sel = np.random.choice(range(n_diff), size=count, replace=False) if n_diff > count else slice(None)
    else:
        val_diff = dist[index_tensor_to_tuple(idx_diff)]    # [nDiff]

        if order == ModeReplaceOrder.SIMILAR:
            sorted_index = val_diff.argsort()
        elif order == ModeReplaceOrder.DIFFERENT:
            sorted_index = val_diff.argsort(descending=True)
        else: raise ValueError(f'unkown replace_order: {order}')

        sel = sorted_index[:count]

    idx_diff_sel = idx_diff[sel, ...]       # [cnt] => [cnt, nDim]
    idx_diff_sel_tp = index_tensor_to_tuple(idx_diff_sel)
    dist[idx_diff_sel_tp] = 0.0
    mask[idx_diff_sel_tp] = False

    if mask.shape != condA.shape:   # cond.shape = [T=77, D=768]
        mask_len = mask.shape[0]
        if   mask_len == condA.shape[0]: mask = mask.unsqueeze(1)
        elif mask_len == condA.shape[1]: mask = mask.unsqueeze(0)
        else: raise ValueError(f'unknown mask.shape: {mask.shape}')
        mask = mask.expand_as(condA)

    return mask * condA + ~mask * condB


def update_img2img_p(p:StableDiffusionProcessing, imgs:List[PILImage], denoising_strength:float=0.75) -> StableDiffusionProcessingImg2Img:
    if isinstance(p, StableDiffusionProcessingImg2Img):
        p.init_images = imgs
        p.denoising_strength = denoising_strength
        return p

    if isinstance(p, StableDiffusionProcessingTxt2Img):
        KNOWN_KEYS = [      # see `StableDiffusionProcessing.__init__()`
            'sd_model',
            'outpath_samples',
            'outpath_grids',
            'prompt',
            'styles',
            'seed',
            'subseed',
            'subseed_strength',
            'seed_resize_from_h',
            'seed_resize_from_w',
            'seed_enable_extras',
            'sampler_name',
            'batch_size',
            'n_iter',
            'steps',
            'cfg_scale',
            'width',
            'height',
            'restore_faces',
            'tiling',
            'do_not_save_samples',
            'do_not_save_grid',
            'extra_generation_params',
            'overlay_images',
            'negative_prompt',
            'eta',
            'do_not_reload_embeddings',
            #'denoising_strength',
            'ddim_discretize',
            's_churn',
            's_tmax',
            's_tmin',
            's_noise',
            'override_settings',
            'sampler_index',
        ]
        kwargs = { k: getattr(p, k) for k in dir(p) if k in KNOWN_KEYS }    # inherit params
        return StableDiffusionProcessingImg2Img(
            init_images=imgs,
            denoising_strength=denoising_strength,
            **kwargs,
        )

def parse_slice(picker:str) -> Union[slice, None]:
    if not picker.strip(): return None
    
    to_int = lambda s: None if not s else int(s)
    segs = [to_int(x.strip()) for x in picker.strip().split(':')]
    
    start, stop, step = None, None, None
    if   len(segs) == 1:        stop,      = segs
    elif len(segs) == 2: start, stop       = segs
    elif len(segs) == 3: start, stop, step = segs
    else: raise ValueError
    
    return slice(start, stop, step)

def get_next_sequence_number(path:str) -> int:
    """ Determines and returns the next sequence number to use when saving an image in the specified directory. The sequence starts at 0. """
    result = -1
    dir = Path(path)
    for file in dir.iterdir():
        if not file.is_dir(): continue
        try:
            num = int(file.name)
            if num > result: result = num
        except ValueError:
            pass
    return result + 1


class Script(Script):

    def title(self):
        return 'Prompt Travel'

    def describe(self):
        return 'Travel from one prompt to another in the latent space.'

    def show(self, is_img2img):
        return True

    def ui(self, is_img2img):
        with gr.Row(variant='compact'):
            mode      = gr.Radio   (label=LABEL_MODE,      value=lambda: DEFAULT_MODE,      choices=CHOICES_MODE)
            lerp_meth = gr.Dropdown(label=LABEL_LERP_METH, value=lambda: DEFAULT_LERP_METH, choices=CHOICES_LERP_METH)

            replace_dim   = gr.Dropdown(label=LABEL_REPLACE_DIM,   value=lambda: DEFAULT_REPLACE_DIM,   choices=CHOICES_REPLACE_DIM,   visible=False)
            replace_order = gr.Dropdown(label=LABEL_REPLACE_ORDER, value=lambda: DEFAULT_REPLACE_ORDER, choices=CHOICES_REPLACE_ORDER, visible=False)

        def switch_mode(mode:str):
            show_meth = Mode(mode) == Mode.LINEAR
            show_repl = Mode(mode) == Mode.REPLACE
            return [gr_show(x) for x in [show_meth, show_repl, show_repl]]
        mode.change(switch_mode, inputs=[mode], outputs=[lerp_meth, replace_dim, replace_order], show_progress=False)

        with gr.Row(variant='compact'):
            genesis     = gr.Dropdown(label=LABEL_GENESIS,     value=lambda: DEFAULT_GENESIS, choices=CHOICES_GENESIS)
            denoise_w   = gr.Slider  (label=LABEL_DENOISE_W,   value=lambda: DEFAULT_DENOISE_W, minimum=0.0, maximum=1.0, visible=False)
            embryo_step = gr.Text    (label=LABEL_EMBRYO_STEP, value=lambda: DEFAULT_EMBRYO_STEP, max_lines=1, visible=False)

        def switch_genesis(genesis:str):
            show_dw = Gensis(genesis) == Gensis.SUCCESSIVE    # 'successive' genesis
            show_es = Gensis(genesis) == Gensis.EMBRYO        # 'embryo' genesis
            return [gr_show(x) for x in [show_dw, show_es]]
        genesis.change(switch_genesis, inputs=[genesis], outputs=[denoise_w, embryo_step], show_progress=False)

        with gr.Row(variant='compact'):
            steps         = gr.Text    (label=LABEL_STEPS,         value=lambda: DEFAULT_STEPS, max_lines=1)
            upscale_meth  = gr.Dropdown(label=LABEL_UPSCALE_METH,  value=lambda: DEFAULT_UPSCALE_METH, choices=CHOICES_UPSCALER)
            upscale_ratio = gr.Slider  (label=LABEL_UPSCALE_RATIO, value=lambda: DEFAULT_UPSCALE_RATIO, minimum=1.0, maximum=16.0, step=0.1)

        with gr.Row(variant='compact'):
            video_fmt  = gr.Dropdown(label=LABEL_VIDEO_FMT,  value=lambda: DEFAULT_VIDEO_FMT, choices=CHOICES_VIDEO_FMT)
            video_fps  = gr.Number  (label=LABEL_VIDEO_FPS,  value=lambda: DEFAULT_VIDEO_FPS)
            video_pad  = gr.Number  (label=LABEL_VIDEO_PAD,  value=lambda: DEFAULT_VIDEO_PAD,  precision=0)
            video_pick = gr.Text    (label=LABEL_VIDEO_PICK, value=lambda: DEFAULT_VIDEO_PICK, max_lines=1)

        with gr.Row(variant='compact'):
            show_debug = gr.Checkbox(label=LABEL_DEBUG, value=lambda: DEFAULT_DEBUG)

        return [mode, lerp_meth,
                replace_dim, replace_order,
                steps, genesis, denoise_w, embryo_step,
                upscale_meth, upscale_ratio,
                video_fmt, video_fps, video_pad, video_pick,
                show_debug]
    
    def run(self, p:StableDiffusionProcessing, 
            mode:str, lerp_meth:str,
            replace_dim:str, replace_order:str,
            steps:str, genesis:str, denoise_w:float, embryo_step:str,
            upscale_meth:str, upscale_ratio:float,
            video_fmt:str, video_fps:float, video_pad:int, video_pick:str,
            show_debug:bool):
        
        # enum looup
        mode: Mode                      = Mode(mode)
        lerp_meth: LerpMethod           = LerpMethod(lerp_meth)
        replace_dim: ModeReplaceDim     = ModeReplaceDim(replace_dim)
        replace_order: ModeReplaceOrder = ModeReplaceOrder(replace_order)
        genesis: Gensis                 = Gensis(genesis)
        video_fmt: VideoFormat          = VideoFormat(video_fmt)

        # Param check & type convert
        if video_pad < 0: return Processed(p, [], p.seed, f'video_pad must >= 0, but got {video_pad}')
        if video_fps < 0: return Processed(p, [], p.seed, f'video_fps must >= 0, but got {video_fps}')
        try: video_slice = parse_slice(video_pick)
        except: return Processed(p, [], p.seed, 'syntax error in video_slice')
        if genesis == Gensis.EMBRYO:
            try: x = float(embryo_step)
            except: return Processed(p, [], p.seed, f'embryo_step is not a number: {embryo_step}')
            if x <= 0: Processed(p, [], p.seed, f'embryo_step must > 0, but got {embryo_step}')
            embryo_step: int = round(x * p.steps if x < 1.0 else x)
            del x

        # Prepare prompts & steps
        prompt_pos = p.prompt.strip()
        if not prompt_pos: return Processed(p, [], p.seed, 'positive prompt should not be empty :(')
        pos_prompts = [p.strip() for p in prompt_pos.split('\n') if p.strip()]
        if len(pos_prompts) == 1: return Processed(p, [], p.seed, 'should specify at least two lines of prompt to travel between :)')
        if genesis == Gensis.EMBRYO and len(pos_prompts) > 2: return Processed(p, [], p.seed, 'currently processing with "embryo" genesis exactly takes 2 prompts :(')
        prompt_neg = p.negative_prompt.strip()
        neg_prompts = [p.strip() for p in prompt_neg.split('\n') if p.strip()]
        if len(neg_prompts) == 0: neg_prompts = ['']
        n_stages = max(len(pos_prompts), len(neg_prompts))
        while len(pos_prompts) < n_stages: pos_prompts.append(pos_prompts[-1])
        while len(neg_prompts) < n_stages: neg_prompts.append(neg_prompts[-1])

        try: steps: List[int] = [int(s.strip()) for s in steps.strip().split(',')]
        except: return Processed(p, [], p.seed, f'cannot parse steps options: {steps}')
        if len(steps) == 1:
            steps = [steps[0]] * (n_stages - 1)
        elif len(steps) != n_stages - 1:
            info = f'stage count mismatch: you have {n_stages} prompt stages, but specified {len(steps)} steps; should assure len(steps) == len(stages) - 1'
            return Processed(p, [], p.seed, info)
        n_frames = sum(steps) + n_stages
        if show_debug:
            print('n_stages:', n_stages)
            print('n_frames:', n_frames)
            print('steps:', steps)
        steps.insert(0, -1)     # fixup the first stage

        # Custom saving path
        travel_path = os.path.join(p.outpath_samples, 'prompt_travel')
        os.makedirs(travel_path, exist_ok=True)
        travel_number = get_next_sequence_number(travel_path)
        self.log_dp = os.path.join(travel_path, f'{travel_number:05}')
        p.outpath_samples = self.log_dp
        os.makedirs(self.log_dp, exist_ok=True)
        self.log_fp = os.path.join(self.log_dp, 'log.txt')

        # Force Batch Count and Batch Size to 1
        p.n_iter     = 1
        p.batch_size = 1

        # Random unified const seed
        p.seed = get_fixed_seed(p.seed)     # fix it to assure all processes using the same major seed
        self.subseed = p.subseed            # stash it to allow using random subseed for each process (when -1)
        if show_debug:
            print('seed:',             p.seed)
            print('subseed:',          p.subseed)
            print('subseed_strength:', p.subseed_strength)

        # Start job
        state.job_count = n_frames
        print(f'Generating {n_frames} images.')

        # Pack parameters
        self.pos_prompts   = pos_prompts
        self.neg_prompts   = neg_prompts
        self.steps         = steps
        self.genesis       = genesis
        self.denoise_w     = denoise_w
        self.embryo_step   = embryo_step
        self.lerp_meth     = lerp_meth
        self.replace_dim   = replace_dim
        self.replace_order = replace_order
        self.show_debug    = show_debug
        self.n_stages      = n_stages
        self.n_frames      = n_frames

        # upscale
        enable_upscale = upscale_meth != 'None' and upscale_ratio > 1.0
        if enable_upscale:
            tgt_w, tgt_h = round(p.width * upscale_ratio), round(p.height * upscale_ratio)
            print(f'>> upscale: ({p.width}, {p.height}) => ({tgt_w}, {tgt_h})')
        
        def save_image_hijack(params:ImageSaveParams):
            img = params.image
            if upscale_ratio > 4:      # must split into two rounds for NN model capatibility
                hf_w, hf_h = round(p.width * 4), round(p.height * 4)
                img = resize_image(0, img, hf_w, hf_h, upscaler_name=upscale_meth)
            img = resize_image(0, img, tgt_w, tgt_h, upscaler_name=upscale_meth)
            params.image = img

        # Dispatch
        if enable_upscale: on_before_image_saved(save_image_hijack)
        process_images_before(p)
        runner = getattr(self, f'run_{mode.value}')
        if not runner: Processed(p, [], p.seed, f'no runner found for mode: {mode.value}')
        images, info = runner(p)
        process_images_after(p)
        if enable_upscale: remove_callbacks_for_function(save_image_hijack)

        # Save video
        if video_fps > 0 and len(images) > 1 and 'ImageSequenceClip' in globals():
            try:
                # arrange frames
                if video_slice:   images = images[video_slice]
                if video_pad > 0: images = [images[0]] * video_pad + images + [images[-1]] * video_pad

                # export video
                seq: List[np.ndarray] = [np.asarray(img) for img in images]
                try:
                    clip = ImageSequenceClip(seq, fps=video_fps)
                except:     # images may have different size
                    clip = concatenate_videoclips([ImageClip(img, duration=1/video_fps) for img in seq], method='compose')
                    clip.fps = video_fps
                fbase = os.path.join(self.log_dp, f'travel-{travel_number:05}')
                if   video_fmt == VideoFormat.MP4:  clip.write_videofile(fbase + '.mp4',  verbose=False, audio=False)
                elif video_fmt == VideoFormat.WEBM: clip.write_videofile(fbase + '.webm', verbose=False, audio=False)
                elif video_fmt == VideoFormat.GIF:  clip.write_gif(fbase + '.gif', loop=True)
            except: print_exc()

        return Processed(p, images, p.seed, info)

    def run_linear(self, p: StableDiffusionProcessing) -> Tuple[List[PILImage], str]:
        lerp_fn     = weighted_sum if self.lerp_meth == LerpMethod.LERP else geometric_slerp
        genesis     = self.genesis
        denoise_w   = self.denoise_w
        pos_prompts = self.pos_prompts
        neg_prompts = self.neg_prompts
        steps       = self.steps
        show_debug  = self.show_debug
        n_stages    = self.n_stages
        n_frames    = self.n_frames

        if genesis == Gensis.EMBRYO:
            return self.run_linear_embryo(p)
        
        initial_info: str = None
        images: List[PILImage] = []

        def gen_image(pos_hidden, neg_hidden, prompts, seeds, subseeds):
            nonlocal images, initial_info, p
            proc = process_images_cond_to_image(p, pos_hidden, neg_hidden, prompts, seeds, subseeds)
            if initial_info is None: initial_info = proc.info
            img = proc.images[0]
            if genesis == Gensis.SUCCESSIVE: p = update_img2img_p(p, proc.images, denoise_w)
            images += [img]

        # Step 1: draw the init image
        if show_debug:
            print(f'[stage 1/{n_stages}]')
            print(f'  pos prompts: {pos_prompts[0]}')
            print(f'  neg prompts: {neg_prompts[0]}')
        p.prompt          = pos_prompts[0]
        p.negative_prompt = neg_prompts[0]
        p.subseed         = self.subseed
        from_pos_hidden, from_neg_hidden, prompts, seeds, subseeds = process_images_prompt_to_cond(p)
        gen_image(from_pos_hidden, from_neg_hidden, prompts, seeds, subseeds)
        
        # travel through stages
        i_frames = 1
        for i in range(1, n_stages):
            if state.interrupted: break

            state.job = f'{i_frames}/{n_frames}'
            state.job_no = i_frames + 1
            i_frames += 1

            # only change target prompts
            if show_debug:
                print(f'[stage {i+1}/{n_stages}]')
                print(f'  pos prompts: {pos_prompts[i]}')
                print(f'  neg prompts: {neg_prompts[i]}')
            p.prompt           = pos_prompts[i]
            p.negative_prompt  = neg_prompts[i]
            p.subseed          = self.subseed
            to_pos_hidden, to_neg_hidden, prompts, seeds, subseeds = process_images_prompt_to_cond(p)

            # Step 2: draw the interpolated images
            is_break_iter = False
            n_inter = steps[i] + 1
            for t in range(1, n_inter):
                if state.interrupted: is_break_iter = True ; break

                alpha = t / n_inter     # [1/T, 2/T, .. T-1/T]
                inter_pos_hidden = lerp_fn(from_pos_hidden, to_pos_hidden, alpha)
                inter_neg_hidden = lerp_fn(from_neg_hidden, to_neg_hidden, alpha)
                gen_image(inter_pos_hidden, inter_neg_hidden, prompts, seeds, subseeds)

            if is_break_iter: break

            # Step 3: draw the fianl stage
            gen_image(to_pos_hidden, to_neg_hidden, prompts, seeds, subseeds)
            
            # move to next stage
            from_pos_hidden, from_neg_hidden = to_pos_hidden, to_neg_hidden

        return images, initial_info

    def run_linear_embryo(self, p: StableDiffusionProcessing) -> Tuple[List[PILImage], str]:
        ''' NOTE: this procedure has special logic, we separate it from run_linear() so far '''

        lerp_fn     = weighted_sum if self.lerp_meth == LerpMethod.LERP else geometric_slerp
        embryo_step = self.embryo_step
        pos_prompts = self.pos_prompts
        n_frames    = self.steps[1] + 2

        initial_info: str = None
        images: List[PILImage] = []
        embryo: Tensor = None       # latent image, the common half-denoised prototype of all frames

        def gen_image(pos_hidden, neg_hidden, prompts, seeds, subseeds, save=True) -> List[PILImage]:
            nonlocal initial_info, p
            do_not_save_samples = p.do_not_save_samples
            if not save: p.do_not_save_samples = True
            proc = process_images_cond_to_image(p, pos_hidden, neg_hidden, prompts, seeds, subseeds)
            p.do_not_save_samples = do_not_save_samples
            if initial_info is None: initial_info = proc.info
            return proc.images

        from modules.script_callbacks import on_cfg_denoiser, remove_callbacks_for_function, CFGDenoiserParams
        def get_embryo_fn(params: CFGDenoiserParams):
            nonlocal embryo, embryo_step
            if params.sampling_step == embryo_step:
                embryo = params.x
        def replace_embryo_fn(params: CFGDenoiserParams):
            nonlocal embryo, embryo_step
            if params.sampling_step == embryo_step:
                params.x.data = embryo
        class denoiser_hijack:
            def __init__(self, callback_fn):
                self.callback_fn = callback_fn
            def __enter__(self):
                on_cfg_denoiser(self.callback_fn)
            def __exit__(self, exc_type, exc_value, exc_traceback):
                remove_callbacks_for_function(self.callback_fn)

        # Step 1: get starting & ending condition
        p.prompt  = pos_prompts[0]
        p.subseed = self.subseed
        from_pos_hidden, neg_hidden, prompts, seeds, subseeds = process_images_prompt_to_cond(p)

        p.prompt  = pos_prompts[1]
        p.subseed = self.subseed
        to_pos_hidden, neg_hidden, prompts, seeds, subseeds = process_images_prompt_to_cond(p)

        # Step 2: get the condition middle-point as embryo then hatch it halfway
        with denoiser_hijack(get_embryo_fn):
            mid_pos_hidden = weighted_sum(from_pos_hidden, to_pos_hidden, 0.5)
            gen_image(mid_pos_hidden, neg_hidden, prompts, seeds, subseeds, save=False)

        try:
            img:PILImage = single_sample_to_image(embryo[0])     # the data is duplicated, just get first item
            img.save(os.path.join(self.log_dp, 'embryo.png'))
        except: pass

        # Step 3: derive the embryo towards each interpolated condition
        with denoiser_hijack(replace_embryo_fn):
            for t in range(0, n_frames+1):
                if state.interrupted: break

                alpha = t / n_frames     # [0, 1/T, 2/T, .. T-1/T, 1]
                inter_pos_hidden = lerp_fn(from_pos_hidden, to_pos_hidden, alpha)
                imgs = gen_image(inter_pos_hidden, neg_hidden, prompts, seeds, subseeds)
                images.extend(imgs)
        
        return images, initial_info

    def run_replace(self, p: StableDiffusionProcessing) -> Tuple[List[PILImage], str]:
        ''' yet another replace method, but do replacing on the condition tensor by token dim or channel dim '''

        genesis       = self.genesis
        denoise_w     = self.denoise_w
        pos_prompts   = self.pos_prompts
        steps         = self.steps
        replace_dim   = self.replace_dim
        replace_order = self.replace_order
        show_debug    = self.show_debug
        n_stages      = self.n_stages
        n_frames      = self.n_frames

        if genesis == Gensis.EMBRYO:
            raise NotImplementedError(f'genesis {genesis.value!r} is only supported in linear mode currently :(')

        initial_info: str = None
        images: List[PILImage] = []

        def gen_image(pos_hidden, neg_hidden, prompts, seeds, subseeds):
            nonlocal images, initial_info, p
            proc = process_images_cond_to_image(p, pos_hidden, neg_hidden, prompts, seeds, subseeds)
            if initial_info is None: initial_info = proc.info
            img = proc.images[0]
            if genesis == Gensis.SUCCESSIVE: p = update_img2img_p(p, proc.images, denoise_w)
            images += [img]

        # Step 1: draw the init image
        if show_debug:
            print(f'[stage 1/{n_stages}]')
            print(f'  pos prompts: {pos_prompts[0]}')
        p.prompt          = pos_prompts[0]
        p.subseed         = self.subseed
        from_pos_hidden, neg_hidden, prompts, seeds, subseeds = process_images_prompt_to_cond(p)
        gen_image(from_pos_hidden, neg_hidden, prompts, seeds, subseeds)
        
        # travel through stages
        i_frames = 1
        for i in range(1, n_stages):
            if state.interrupted: break

            state.job = f'{i_frames}/{n_frames}'
            state.job_no = i_frames + 1
            i_frames += 1

            # only change target prompts
            if show_debug:
                print(f'[stage {i+1}/{n_stages}]')
                print(f'  pos prompts: {pos_prompts[i]}')
            p.prompt           = pos_prompts[i]
            p.subseed          = self.subseed
            to_pos_hidden, neg_hidden, prompts, seeds, subseeds = process_images_prompt_to_cond(p)

            # ========== ↓↓↓ major differences from run_linear() ↓↓↓ ==========
            
            # decide change portion in each iter
            L1 = torch.abs(cond_get(from_pos_hidden) - cond_get(to_pos_hidden))
            if   replace_dim == ModeReplaceDim.RANDOM:
                dist = L1                  # [T=77, D=768]
            elif replace_dim == ModeReplaceDim.TOKEN:
                dist = L1.mean(axis=1)     # [T=77]
            elif replace_dim == ModeReplaceDim.CHANNEL:
                dist = L1.mean(axis=0)     # [D=768]
            else: raise ValueError(f'unknown replace_dim: {replace_dim}')
            mask = dist > EPS
            dist = torch.where(mask, dist, 0.0)
            n_diff = mask.sum().item()            # when value differs we have mask==True
            n_inter = steps[i] + 1
            replace_count = int(n_diff / n_inter) + 1    # => accumulative modifies [1/T, 2/T, .. T-1/T] of total cond

            # Step 2: draw the replaced images
            inter_pos_hidden = from_pos_hidden
            is_break_iter = False
            for _ in range(1, n_inter):
                if state.interrupted: is_break_iter = True ; break

                inter_pos_hidden = replace_until_match(inter_pos_hidden, to_pos_hidden, replace_count, dist=dist, order=replace_order)
                gen_image(inter_pos_hidden, neg_hidden, prompts, seeds, subseeds)
            
            # ========== ↑↑↑ major differences from run_linear() ↑↑↑ ==========

            if is_break_iter: break

            # Step 3: draw the fianl stage
            gen_image(to_pos_hidden, neg_hidden, prompts, seeds, subseeds)
            
            # move to next stage
            from_pos_hidden = to_pos_hidden

        return images, initial_info
