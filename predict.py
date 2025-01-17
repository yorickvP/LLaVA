from typing import Optional
import time
import subprocess
from threading import Thread
from io import BytesIO
import shutil
import tarfile
import os

from PIL import Image
import requests
import torch
from transformers.generation.streamers import TextIteratorStreamer
from cog import BasePredictor, Input, Path, ConcatenateIterator

from llava.constants import IMAGE_TOKEN_INDEX, DEFAULT_IMAGE_TOKEN
from llava.conversation import conv_templates, SeparatorStyle
from llava.model.builder import load_pretrained_model
from llava.utils import disable_torch_init
from llava.mm_utils import tokenizer_image_token, KeywordsStoppingCriteria
from file_utils import is_url, download_weights, download_json, DEFAULT_WEIGHTS

# we don't use the huggingface hub cache, but we need to set this to a local folder
os.environ["HUGGINGFACE_HUB_CACHE"] = os.getcwd() + "/models"

class Predictor(BasePredictor):
    def setup(self, weights: Optional[Path] = None) -> None:
        """Load the model into memory to make running multiple predictions efficient. 
        
        The parameter `weights` can be set with environment variable COG_WEIGHTS or with cog predict -e [your weights here]
        """
        # download base models
        for weight in DEFAULT_WEIGHTS:
            download_weights(weight["src"], weight["dest"], weight["files"])
        disable_torch_init()

        # custom weights
        if weights is not None and str(weights) != "weights":
            print(f"Loading custom LLaVA lora model: {weights}...")
            
            # remove folder if it already exists
            custom_weights_dir = Path("/src/custom_weights")
            if custom_weights_dir.exists():
                shutil.rmtree(custom_weights_dir)
            
            # download custom weights from URL
            custom_weights_dir.mkdir(parents=True, exist_ok=True)
            weights_url = str(weights)
            download_location = custom_weights_dir / "custom_weights.tar"
            subprocess.check_call(["pget", str(weights_url), str(download_location)], close_fds=False)

            # extract tar file
            custom_weights_file = tarfile.open(download_location)
            custom_weights_file.extractall(path=custom_weights_dir)

            self.tokenizer, self.model, self.image_processor, self.context_len = load_pretrained_model(custom_weights_dir, model_name="llava-v1.5-13b-custom-lora", model_base="liuhaotian/llava-v1.5-13b", load_8bit=False, load_4bit=False)

        else:
            print(f"Loading base LLaVA model...")
            self.tokenizer, self.model, self.image_processor, self.context_len = load_pretrained_model("liuhaotian/llava-v1.5-13b", model_name="llava-v1.5-13b", model_base=None, load_8bit=False, load_4bit=False)

    def predict(
        self,
        image: Path = Input(description="Input image"),
        prompt: str = Input(description="Prompt to use for text generation"),
        top_p: float = Input(description="When decoding text, samples from the top p percentage of most likely tokens; lower to ignore less likely tokens", ge=0.0, le=1.0, default=1.0),
        temperature: float = Input(description="Adjusts randomness of outputs, greater than 1 is random and 0 is deterministic", default=0.2, ge=0.0),
        max_tokens: int = Input(description="Maximum number of tokens to generate. A word is generally 2-3 tokens", default=1024, ge=0),
    ) -> ConcatenateIterator[str]:
        """Run a single prediction on the model"""
    
        conv_mode = "llava_v1"
        conv = conv_templates[conv_mode].copy()
    
        image_data = load_image(str(image))
        image_tensor = self.image_processor.preprocess(image_data, return_tensors='pt')['pixel_values'].half().cuda()
    
        # loop start
    
        # just one turn, always prepend image token
        inp = DEFAULT_IMAGE_TOKEN + '\n' + prompt
        conv.append_message(conv.roles[0], inp)

        conv.append_message(conv.roles[1], None)
        prompt = conv.get_prompt()
    
        input_ids = tokenizer_image_token(prompt, self.tokenizer, IMAGE_TOKEN_INDEX, return_tensors='pt').unsqueeze(0).cuda()
        stop_str = conv.sep if conv.sep_style != SeparatorStyle.TWO else conv.sep2
        keywords = [stop_str]
        stopping_criteria = KeywordsStoppingCriteria(keywords, self.tokenizer, input_ids)
        streamer = TextIteratorStreamer(self.tokenizer, skip_prompt=True, timeout=20.0)
    
        with torch.inference_mode():
            thread = Thread(target=self.model.generate, kwargs=dict(
                inputs=input_ids,
                images=image_tensor,
                do_sample=True,
                temperature=temperature,
                top_p=top_p,
                max_new_tokens=max_tokens,
                streamer=streamer,
                use_cache=True,
                stopping_criteria=[stopping_criteria]))
            thread.start()
            # workaround: second-to-last token is always " "
            # but we want to keep it if it's not the second-to-last token
            prepend_space = False
            for new_text in streamer:
                if new_text == " ":
                    prepend_space = True
                    continue
                if new_text.endswith(stop_str):
                    new_text = new_text[:-len(stop_str)].strip()
                    prepend_space = False
                elif prepend_space:
                    new_text = " " + new_text
                    prepend_space = False
                if len(new_text):
                    yield new_text
            if prepend_space:
                yield " "
            thread.join()
    

def load_image(image_file):
    if image_file.startswith('http') or image_file.startswith('https'):
        response = requests.get(image_file)
        image = Image.open(BytesIO(response.content)).convert('RGB')
    else:
        image = Image.open(image_file).convert('RGB')
    return image

