import logging
import os
import random
from pathlib import Path
from typing import Any, NamedTuple

from langchain.embeddings import CacheBackedEmbeddings
from langchain.prompts import (
    AIMessagePromptTemplate,
    ChatPromptTemplate,
    HumanMessagePromptTemplate,
    SystemMessagePromptTemplate,
)
from langchain.storage import LocalFileStore
from langchain_community.embeddings import HuggingFaceEmbeddings
from langchain_community.llms import HuggingFaceHub, HuggingFacePipeline
from langchain_community.vectorstores import FAISS
from langchain_core.embeddings import Embeddings
from langchain_core.example_selectors import SemanticSimilarityExampleSelector
from langchain_core.example_selectors.base import BaseExampleSelector
from langchain_core.language_models.base import BaseLanguageModel
from langchain_core.messages import BaseMessage
from langchain_core.prompts.chat import (
    BaseChatPromptTemplate,
    BaseMessagePromptTemplate,
)
from langchain_core.prompts.few_shot import FewShotChatMessagePromptTemplate
from langchain_openai import AzureChatOpenAI
from transformers import AutoModelForCausalLM, AutoTokenizer, pipeline

logger = logging.getLogger(__name__)


SUFFIX = "Make a detailed plan to for a graphic design for the following request: '{intention}'."
CHAT_SYSTEM_MESSAGE = "You are an helpful AI Assistant. {format_instructions}"


class Example(NamedTuple):
    # note: intention and detail are generated by .json()
    intention: str
    detail: str | None = None
    id: str | None = None


class RandomExampleSelector(BaseExampleSelector):  # type: ignore
    def __init__(self, examples: list[Any], k: int) -> None:
        self.examples = examples
        self.k = k

    def add_example(self, example: Any) -> None:
        self.examples.append(example)

    def select_examples(self, *args: Any, **kwargs: Any) -> list[Any]:
        return random.sample(population=self.examples, k=self.k)


def initialize_embeddings(embeddings_cache_name: str) -> Embeddings:
    """
    https://python.langchain.com/docs/modules/data_connection/text_embedding/caching_embeddings
    """
    embedder = HuggingFaceEmbeddings()
    namespace = "hfemb_default"
    if embeddings_cache_name is not None:
        logger.info(f"Use {embeddings_cache_name=} for embeddings cache.")
        path = Path(embeddings_cache_name)
        if not path.exists():
            parent_path = path.parent
            if not parent_path.exists():
                parent_path.mkdir(parents=True, exist_ok=True)

        store = LocalFileStore(embeddings_cache_name)
        cached_embedder = CacheBackedEmbeddings.from_bytes_store(
            embedder, store, namespace=namespace
        )
        return cached_embedder
    else:
        return embedder  # type: ignore


def setup_model(
    model_id: str | None = None,
    repo_id: str | None = None,
    azure_openai_model_name: str | None = None,
) -> BaseLanguageModel:
    if model_id is not None:
        tokenizer = AutoTokenizer.from_pretrained(model_id)
        _model = AutoModelForCausalLM.from_pretrained(model_id)
        pipe = pipeline(
            "text-generation", model=_model, tokenizer=tokenizer, max_new_tokens=64
        )
        model = HuggingFacePipeline(pipeline=pipe)
    elif repo_id is not None:
        model = HuggingFaceHub(
            repo_id=repo_id, model_kwargs={"temperature": 0, "max_length": 64}
        )
    elif azure_openai_model_name is not None:
        model = AzureChatOpenAI(
            deployment_name=azure_openai_model_name,
            model_name=azure_openai_model_name,
            openai_api_version=os.getenv("AZURE_OPENAI_API_VERSION"),
        )  # type: ignore
    else:
        raise NotImplementedError("No model is specified.")
    return model


def setup_prompt(
    examples: list[Example],
    format_instructions: str,
    few_shot_by: str = "random",
    k: int = 5,
    embeddings_cache_name: str | None = None,
) -> ChatPromptTemplate:
    messages: list[BaseMessagePromptTemplate | BaseMessage | BaseChatPromptTemplate] = [
        SystemMessagePromptTemplate.from_template(CHAT_SYSTEM_MESSAGE),
    ]
    if k > 0:
        assert len(examples) > 0
        template_kwargs = {}
        # k-shot setting
        template_kwargs["example_prompt"] = ChatPromptTemplate(
            messages=[
                HumanMessagePromptTemplate.from_template(SUFFIX),
                AIMessagePromptTemplate.from_template("{detail}"),
            ],
            input_variables=["intention", "detail"],
        )
        examples = [
            {key: getattr(e, key) for key in ["intention", "detail"]} for e in examples
        ]  # type: ignore
        template_kwargs.update(
            setup_examples_or_its_selector(
                examples=examples,
                few_shot_by=few_shot_by,
                k=k,
                embeddings_cache_name=embeddings_cache_name,
            )
        )
        messages.append(FewShotChatMessagePromptTemplate(**template_kwargs))  # type: ignore
    messages.append(HumanMessagePromptTemplate.from_template(SUFFIX))

    prompt = ChatPromptTemplate(
        messages=messages,
        input_variables=["intention"],
        partial_variables={"format_instructions": format_instructions},
    )

    return prompt


def setup_examples_or_its_selector(
    examples: list[Example],
    few_shot_by: str = "random",
    k: int = 5,
    embeddings_cache_name: str | None = None,
) -> dict[str, Any]:
    kwargs = {}
    if few_shot_by == "similarity":
        assert (
            embeddings_cache_name is not None
        ), "embeddings_cache_name is required for retrieval based on sentence similarity between the extracted embeddings."
        logger.info(
            "Initializing similarity-based example selector (start): (taking long time for the first time) ..."
        )
        embeddings = initialize_embeddings(embeddings_cache_name)
        kwargs["example_selector"] = SemanticSimilarityExampleSelector.from_examples(
            examples=examples,  # type: ignore
            embeddings=embeddings,
            vectorstore_cls=FAISS,
            k=k,
            input_keys=["intention"],
        )
        logger.info("Initializing similarity-based example selector (finished)")
    elif few_shot_by == "random":
        kwargs["example_selector"] = RandomExampleSelector(examples=examples, k=k)  # type: ignore
    elif few_shot_by == "fixed":
        kwargs["examples"] = random.sample(population=examples, k=k)  # type: ignore
    else:
        raise NotImplementedError
    return kwargs