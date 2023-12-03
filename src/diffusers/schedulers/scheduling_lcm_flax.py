import math
from dataclasses import dataclass
from typing import List, Optional, Tuple, Union

import flax
import jax.numpy as jnp

from ..configuration_utils import ConfigMixin, register_to_config
from ..utils import BaseOutput, logging
from ..utils.torch_utils import randn_tensor
from .scheduling_utils_flax import (
    CommonSchedulerState,
    FlaxKarrasDiffusionSchedulers,
    FlaxSchedulerMixin,
    FlaxSchedulerOutput,
    add_noise_common,
    get_velocity_common,
)


logger = logging.get_logger(__name__)  # pylint: disable=invalid-name


@flax.struct.dataclass
class LCMSchedulerState:
    common: CommonSchedulerState
    final_alpha_cumprod: jnp.ndarray

    # setable values
    init_noise_sigma: jnp.ndarray
    timesteps: jnp.ndarray
    num_inference_steps: Optional[int] = None
    step_index: Optional[int] = None 

    @classmethod
    def create(
        cls,
        common: CommonSchedulerState,
        final_alpha_cumprod: jnp.ndarray,
        init_noise_sigma: jnp.ndarray,
        timesteps: jnp.ndarray,
    ):
        return cls(
            common=common,
            final_alpha_cumprod=final_alpha_cumprod,
            init_noise_sigma=init_noise_sigma,
            timesteps=timesteps,
        )
        

@dataclass
class FlaxLCMSchedulerOutput(FlaxSchedulerOutput):
    state: LCMSchedulerState


class FlaxLCMScheduler(FlaxSchedulerMixin, ConfigMixin):
    """
    `LCMScheduler` extends the denoising procedure introduced in denoising diffusion probabilistic models (DDPMs) with
    non-Markovian guidance.

    This model inherits from [`SchedulerMixin`] and [`ConfigMixin`]. [`~ConfigMixin`] takes care of storing all config
    attributes that are passed in the scheduler's `__init__` function, such as `num_train_timesteps`. They can be
    accessed via `scheduler.config.num_train_timesteps`. [`SchedulerMixin`] provides general loading and saving
    functionality via the [`SchedulerMixin.save_pretrained`] and [`~SchedulerMixin.from_pretrained`] functions.

    Args:
        num_train_timesteps (`int`, defaults to 1000):
            The number of diffusion steps to train the model.
        beta_start (`float`, defaults to 0.0001):
            The starting `beta` value of inference.
        beta_end (`float`, defaults to 0.02):
            The final `beta` value.
        beta_schedule (`str`, defaults to `"linear"`):
            The beta schedule, a mapping from a beta range to a sequence of betas for stepping the model. Choose from
            `linear`, `scaled_linear`, or `squaredcos_cap_v2`.
        trained_betas (`np.ndarray`, *optional*):
            Pass an array of betas directly to the constructor to bypass `beta_start` and `beta_end`.
        original_inference_steps (`int`, *optional*, defaults to 50):
            The default number of inference steps used to generate a linearly-spaced timestep schedule, from which we
            will ultimately take `num_inference_steps` evenly spaced timesteps to form the final timestep schedule.
        clip_sample (`bool`, defaults to `True`):
            Clip the predicted sample for numerical stability.
        clip_sample_range (`float`, defaults to 1.0):
            The maximum magnitude for sample clipping. Valid only when `clip_sample=True`.
        set_alpha_to_one (`bool`, defaults to `True`):
            Each diffusion step uses the alphas product value at that step and at the previous one. For the final step
            there is no previous alpha. When this option is `True` the previous alpha product is fixed to `1`,
            otherwise it uses the alpha value at step 0.
        steps_offset (`int`, defaults to 0):
            An offset added to the inference steps. You can use a combination of `offset=1` and
            `set_alpha_to_one=False` to make the last step use step 0 for the previous alpha product like in Stable
            Diffusion.
        prediction_type (`str`, defaults to `epsilon`, *optional*):
            Prediction type of the scheduler function; can be `epsilon` (predicts the noise of the diffusion process),
            `sample` (directly predicts the noisy sample`) or `v_prediction` (see section 2.4 of [Imagen
            Video](https://imagen.research.google/video/paper.pdf) paper).
        thresholding (`bool`, defaults to `False`):
            Whether to use the "dynamic thresholding" method. This is unsuitable for latent-space diffusion models such
            as Stable Diffusion.
        dynamic_thresholding_ratio (`float`, defaults to 0.995):
            The ratio for the dynamic thresholding method. Valid only when `thresholding=True`.
        sample_max_value (`float`, defaults to 1.0):
            The threshold value for dynamic thresholding. Valid only when `thresholding=True`.
        timestep_spacing (`str`, defaults to `"leading"`):
            The way the timesteps should be scaled. Refer to Table 2 of the [Common Diffusion Noise Schedules and
            Sample Steps are Flawed](https://huggingface.co/papers/2305.08891) for more information.
        timestep_scaling (`float`, defaults to 10.0):
            The factor the timesteps will be multiplied by when calculating the consistency model boundary conditions
            `c_skip` and `c_out`. Increasing this will decrease the approximation error (although the approximation
            error at the default of `10.0` is already pretty small).
        rescale_betas_zero_snr (`bool`, defaults to `False`):
            Whether to rescale the betas to have zero terminal SNR. This enables the model to generate very bright and
            dark samples instead of limiting it to samples with medium brightness. Loosely related to
            [`--offset_noise`](https://github.com/huggingface/diffusers/blob/74fd735eb073eb1d774b1ab4154a0876eb82f055/examples/dreambooth/train_dreambooth.py#L506).
    """

    order = 1
    
    _compatibles = [e.name for e in FlaxKarrasDiffusionSchedulers]

    dtype: jnp.dtype

    @property
    def has_state(self):
        return True

    @register_to_config
    def __init__(
        self,
        num_train_timesteps: int = 1000,
        beta_start: float = 0.00085,
        beta_end: float = 0.012,
        beta_schedule: str = "scaled_linear", 
        trained_betas: Optional[Union[jnp.ndarray, List[float]]] = None,
        original_inference_steps: int = 50,  # LCM scheduler
        clip_sample: bool = False,           # LCM scheduler
        clip_sample_range: float = 1.0,      # LCM scheduler
        set_alpha_to_one: bool = True,
        steps_offset: int = 0,
        prediction_type: str = "epsilon",
        thresholding: bool = False,                # LCM scheduler
        dynamic_thresholding_ratio: float = 0.995, # LCM scheduler
        sample_max_value: float = 1.0,             # LCM scheduler
        timestep_spacing: str = "leading",         # LCM scheduler
        timestep_scaling: float = 10.0,            # LCM scheduler
        rescale_betas_zero_snr: bool = False,      # LCM scheduler
        dtype: jnp.dtype = jnp.float32,

    ):
        self.dtype = dtype
        

    # Copied from diffusers.schedulers.scheduling_euler_discrete.EulerDiscreteScheduler._init_step_index
    def _init_step_index(self, timestep):
        #if isinstance(timestep, jnp.ndarray):
        #    timestep = timestep.to(self.timesteps.device)

        index_candidates = (self.timesteps == timestep).nonzero()
        
        # The sigma index that is taken for the **very** first `step`
        # is always the second index (or the last index if there is only 1)
        # This way we can ensure we don't accidentally skip a sigma in
        # case we start in the middle of the denoising schedule (e.g. for image-to-image)
        if len(index_candidates) > 1:
            step_index = index_candidates[1]
        else:
            step_index = index_candidates[0]

        self._step_index = step_index.item()
 
  
    def create_state(self, common: Optional[CommonSchedulerState] = None) -> LCMSchedulerState:
        if common is None:
            common = CommonSchedulerState.create(self)

        # At every step in ddim, we are looking into the previous alphas_cumprod
        # For the final step, there is no previous alphas_cumprod because we are already at 0
        # `set_alpha_to_one` decides whether we set this parameter simply to one or
        # whether we use the final alpha of the "non-previous" one.
        final_alpha_cumprod = (
            jnp.array(1.0, dtype=self.dtype) if self.config.set_alpha_to_one else common.alphas_cumprod[0]
        )

        # standard deviation of the initial noise distribution
        init_noise_sigma = jnp.array(1.0, dtype=self.dtype)

        timesteps = jnp.arange(0, self.config.num_train_timesteps).round()[::-1]

        return LCMSchedulerState.create(
            common=common,
            final_alpha_cumprod=final_alpha_cumprod,
            init_noise_sigma=init_noise_sigma,
            timesteps=timesteps
        )
       
   
    def scale_model_input(self, state: LCMSchedulerState, sample: jnp.ndarray, timestep: Optional[int] = None) -> jnp.ndarray:
        """
        Ensures interchangeability with schedulers that need to scale the denoising model input depending on the
        current timestep.

        Args:
            sample (`torch.FloatTensor`):
                The input sample.
            timestep (`int`, *optional*):
                The current timestep in the diffusion chain.
        Returns:
            `torch.FloatTensor`:
                A scaled input sample.
        """
        return sample
    
    # Copied from diffusers.schedulers.scheduling_ddim_flax
    def set_timesteps(
        self, state: LCMSchedulerState, num_inference_steps: int, shape: Tuple = ()
    ) -> LCMSchedulerState:
        """
        Sets the discrete timesteps used for the diffusion chain. Supporting function to be run before inference.

        Args:
            state (`DDIMSchedulerState`):
                the `FlaxDDIMScheduler` state data class instance.
            num_inference_steps (`int`):
                the number of diffusion steps used when generating samples with a pre-trained model.
        """
        step_ratio = self.config.num_train_timesteps // num_inference_steps
        # creates integer timesteps by multiplying by ratio
        # rounding to avoid issues when num_inference_step is power of 3
        timesteps = (jnp.arange(0, num_inference_steps) * step_ratio).round()[::-1] + self.config.steps_offset

        return state.replace(
            num_inference_steps=num_inference_steps,
            timesteps=timesteps,
        )
        
    # Copied from diffusers.schedulers.scheduling_ddim_flax
    def _get_variance(self, state: LCMSchedulerState, timestep, prev_timestep):
        alpha_prod_t = state.common.alphas_cumprod[timestep]
        alpha_prod_t_prev = jnp.where(
            prev_timestep >= 0, state.common.alphas_cumprod[prev_timestep], state.final_alpha_cumprod
        )
        beta_prod_t = 1 - alpha_prod_t
        beta_prod_t_prev = 1 - alpha_prod_t_prev

        variance = (beta_prod_t_prev / beta_prod_t) * (1 - alpha_prod_t / alpha_prod_t_prev)

        return variance


    def get_scalings_for_boundary_condition_discrete(self, timestep):
        self.sigma_data = 0.5  # Default: 0.5
        scaled_timestep = timestep * self.config.timestep_scaling

        c_skip = self.sigma_data**2 / (scaled_timestep**2 + self.sigma_data**2)
        c_out = scaled_timestep / (scaled_timestep**2 + self.sigma_data**2) ** 0.5
        return c_skip, c_out

    def step(
        self,
        state: LCMSchedulerState,
        model_output: jnp.ndarray,
        timestep: int,
        sample: jnp.ndarray,
        #generator: Optional[torch.Generator] = None,
        return_dict: bool = True,
    ) -> Union[FlaxLCMSchedulerOutput, Tuple]:
        """
        Predict the sample from the previous timestep by reversing the SDE. This function propagates the diffusion
        process from the learned model outputs (most often the predicted noise).

        Args:
            model_output (`torch.FloatTensor`):
                The direct output from learned diffusion model.
            timestep (`float`):
                The current discrete timestep in the diffusion chain.
            sample (`torch.FloatTensor`):
                A current instance of a sample created by the diffusion process.
            generator (`torch.Generator`, *optional*):
                A random number generator.
            return_dict (`bool`, *optional*, defaults to `True`):
                Whether or not to return a [`~schedulers.scheduling_lcm.LCMSchedulerOutput`] or `tuple`.
        Returns:
            [`~schedulers.scheduling_utils.LCMSchedulerOutput`] or `tuple`:
                If return_dict is `True`, [`~schedulers.scheduling_lcm.LCMSchedulerOutput`] is returned, otherwise a
                tuple is returned where the first element is the sample tensor.
        """
        if state.num_inference_steps is None:
            raise ValueError(
                "Number of inference steps is 'None', you need to run 'set_timesteps' after creating the scheduler"
            )

        if state.step_index is None:
            self._init_step_index(timestep)

        # 1. get previous step value
        prev_step_index = self.step_index + 1
        if prev_step_index < len(self.timesteps):
            prev_timestep = self.timesteps[prev_step_index]
        else:
            prev_timestep = timestep

        # 2. compute alphas, betas
        alpha_prod_t = self.alphas_cumprod[timestep]
        alpha_prod_t_prev = self.alphas_cumprod[prev_timestep] if prev_timestep >= 0 else self.final_alpha_cumprod

        beta_prod_t = 1 - alpha_prod_t
        beta_prod_t_prev = 1 - alpha_prod_t_prev

        # 3. Get scalings for boundary conditions
        c_skip, c_out = self.get_scalings_for_boundary_condition_discrete(timestep)

        # 4. Compute the predicted original sample x_0 based on the model parameterization
        if self.config.prediction_type == "epsilon":  # noise-prediction
            predicted_original_sample = (sample - beta_prod_t.sqrt() * model_output) / alpha_prod_t.sqrt()
        elif self.config.prediction_type == "sample":  # x-prediction
            predicted_original_sample = model_output
        elif self.config.prediction_type == "v_prediction":  # v-prediction
            predicted_original_sample = alpha_prod_t.sqrt() * sample - beta_prod_t.sqrt() * model_output
        else:
            raise ValueError(
                f"prediction_type given as {self.config.prediction_type} must be one of `epsilon`, `sample` or"
                " `v_prediction` for `LCMScheduler`."
            )

        # 5. Clip or threshold "predicted x_0"
        # if self.config.thresholding:
        #     predicted_original_sample = self._threshold_sample(predicted_original_sample)
        # elif self.config.clip_sample:
        #     predicted_original_sample = predicted_original_sample.clamp(
        #         -self.config.clip_sample_range, self.config.clip_sample_range
        #     )

        # 6. Denoise model output using boundary conditions
        denoised = c_out * predicted_original_sample + c_skip * sample

        # 7. Sample and inject noise z ~ N(0, I) for MultiStep Inference
        # Noise is not used on the final timestep of the timestep schedule.
        # This also means that noise is not used for one-step sampling.
        # TODO
        # if self.step_index != self.num_inference_steps - 1:
        #     noise = randn_tensor(
        #         model_output.shape, generator=generator, device=model_output.device, dtype=denoised.dtype
        #     )
        #     prev_sample = alpha_prod_t_prev.sqrt() * denoised + beta_prod_t_prev.sqrt() * noise
        # else:
        prev_sample = denoised

        # # upon completion increase step index by one
        # self._step_index += 1

        if not return_dict:
            return (prev_sample, state)

        return FlaxLCMSchedulerOutput(prev_sample=prev_sample, denoised=denoised)

    # Copied from diffusers.schedulers.scheduling_ddpm.DDPMScheduler.add_noise
    def add_noise(
        self,
        state: LCMSchedulerState,
        original_samples: jnp.ndarray,
        noise: jnp.ndarray,
        timesteps: jnp.ndarray,
    ) -> jnp.ndarray:
        return add_noise_common(state.common, original_samples, noise, timesteps)


    # Copied from diffusers.schedulers.scheduling_ddpm.DDPMScheduler.get_velocity
    def get_velocity(
        self, state: LCMSchedulerState, sample: jnp.ndarray, noise: jnp.ndarray, timesteps: jnp.ndarray
    ) -> jnp.ndarray:
        return get_velocity_common(state.common, sample, noise, timesteps)


    def __len__(self):
        return self.config.num_train_timesteps

    # Copied from diffusers.schedulers.scheduling_ddpm.DDPMScheduler.previous_timestep
    # def previous_timestep(self, timestep):
    #     if self.custom_timesteps:
    #         index = (self.timesteps == timestep).nonzero(as_tuple=True)[0][0]
    #         if index == self.timesteps.shape[0] - 1:
    #             prev_t = torch.tensor(-1)
    #         else:
    #             prev_t = self.timesteps[index + 1]
    #     else:
    #         num_inference_steps = (
    #             self.num_inference_steps if self.num_inference_steps else self.config.num_train_timesteps
    #         )
    #         prev_t = timestep - self.config.num_train_timesteps // num_inference_steps

    #     return prev_t
