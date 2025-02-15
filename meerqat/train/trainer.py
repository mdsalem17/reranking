"""Usage: trainer.py <config>"""
from docopt import docopt
import json
from pathlib import Path
import warnings
from tqdm import tqdm
import collections
import sys
import logging
import humanize
from PIL import Image 

import numpy as np
import torch
from torch import nn
from torch.autograd import set_detect_anomaly
from torch.utils.data.dataset import IterableDataset
import torch.distributed as dist

from transformers import ViTFeatureExtractor, ViTModel, ViltFeatureExtractor
from transformers import Trainer, TrainingArguments, trainer_callback, logging as t_logging
from transformers.trainer_callback import TrainerState
from datasets import load_from_disk, load_metric
from transformers.deepspeed import deepspeed_init
from transformers.file_utils import WEIGHTS_NAME, is_torch_tpu_available
from transformers.trainer_pt_utils import (
    IterableDatasetShard,
    find_batch_size,
    nested_concat,
    nested_numpify,
    nested_detach
)
from transformers.trainer_utils import EvalLoopOutput, denumpify_detensorize
if is_torch_tpu_available():
    import torch_xla.distributed.parallel_loader as pl

from meerqat.data.loading import load_pretrained_in_kwargs
from meerqat.models.qa import get_best_spans, format_predictions_for_squad
from meerqat.train import metrics as metric_functions


logging.basicConfig()
logger = logging.getLogger(__name__)


def max_memory_usage(human=False):
    logs = {}
    for i in range(torch.cuda.device_count()):
        device = f"cuda:{i}"
        value = torch.cuda.max_memory_allocated(device)
        if human:
            value = humanize.naturalsize(value, gnu=True)
        logs[f"max_memory_{device}"] = value
    return logs


def json_load(path):
    with open(path, 'r') as fid:
        data_ = json.load(fid)
    return data_


class MeerqatTrainer(Trainer):
    """Base class for all trainers. Should be very similar to Trainer"""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.prediction_file_name = "predictions.json"
        self.metrics_file_name = "metrics.json"

    def log(self, logs: dict) -> None:
        """Adds memory usage to the logs"""
        logs.update(max_memory_usage())
        return super().log(logs)

    def write_predictions(self, predictions, resume_from_checkpoint):
        if isinstance(predictions, (list, dict)):
            with open(resume_from_checkpoint/self.prediction_file_name, "w") as file:
                json.dump(predictions, file)
        else:
            raise NotImplementedError()

    def write_metrics(self, metrics, resume_from_checkpoint):
        print(metrics)
        with open(resume_from_checkpoint/self.metrics_file_name, "w") as file:
            json.dump(metrics, file)


class QuestionAnsweringTrainer(MeerqatTrainer):
    """
    Base class for Question Answering trainers. Should work for both IR and RC.

        Overrides some methods because we need to create the batch of questions and passages on-the-fly

    Because the inputs should be shaped like (N * M, L), where:
            N - number of distinct questions
            M - number of passages per question in a batch
            L - sequence length

    Parameters
    ----------
    *args, **kwargs: additional arguments are passed to MeerqatTrainer
    kb: str
        path towards the knowledge base (Dataset) used to get the passages
    M: int, optional
        Number of passages (relevant or irrelevant) per question in a batch
        Defaults to 24
    n_relevant_passages: int, optional
        Defaults to 1
    search_key: str, optional
        This column in the dataset suffixed by '_indices' and '_scores' should hold the result of information retrieval
        used during evaluation (e.g. the output of ir.search)
        Suffixed by "_provenance_indices" and "_irrelevant_indices" it should hold:
            1. the union of relevant search and provenance_indices
            2. irrelevant results from the search
        used during training (according to M and n_relevant_passages)
        Defaults to 'search'
    tokenization_kwargs: dict, optional
        To be passed to self.tokenizer
    """
    def __init__(self, *args, kb, M=24, n_relevant_passages=1, search_key='search', tokenization_kwargs=None, **kwargs):
        super().__init__(*args, **kwargs)
        assert self.tokenizer is not None
        self.kb = load_from_disk(kb)
        self.M = M
        assert n_relevant_passages <= M
        self.n_relevant_passages = n_relevant_passages
        self.search_key = search_key
        default_tokenization_kwargs = dict(return_tensors='pt', padding='max_length', truncation=True)
        if tokenization_kwargs is None:
            tokenization_kwargs = {}
        default_tokenization_kwargs.update(tokenization_kwargs)
        self.tokenization_kwargs = default_tokenization_kwargs
        self.data_collator = self.collate_fn

        # we need those ‘un-used’ columns to actually create the batch the model will use
        if self.args.remove_unused_columns:
            warnings.warn(f'Setting args.remove_unused_columns to False')
            self.args.remove_unused_columns = False

    def get_training_passages(self, item):
        relevant_passages = []
        all_relevant_indices = item[self.search_key+"_provenance_indices"]
        n_relevant = min(len(all_relevant_indices), self.n_relevant_passages)
        if n_relevant > 0:
            relevant_indices = np.random.choice(all_relevant_indices, n_relevant, replace=False)
            if len(relevant_indices) > 0:
                relevant_passages = self.kb.select(relevant_indices)['passage']
        irrelevant_passages = []
        all_irrelevant_indices = item[self.search_key+"_irrelevant_indices"]
        n_irrelevant = min(len(all_irrelevant_indices), self.M-self.n_relevant_passages)
        if n_irrelevant > 0:
            irrelevant_indices = np.random.choice(all_irrelevant_indices, n_irrelevant, replace=False)
            if len(irrelevant_indices) > 0:
                irrelevant_passages = self.kb.select(irrelevant_indices)['passage']
        elif n_relevant <= 0:
            warnings.warn(f"Didn't find any passage for question {item['id']}")
        return relevant_passages, irrelevant_passages



class DPRBiEncoderTrainer(QuestionAnsweringTrainer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.log_softmax = nn.LogSoftmax(1)
        self.loss_fct = nn.NLLLoss(reduction='mean')
        assert self.n_relevant_passages == 1

    def collate_fn(self, items):
        """
        Collate batch so that each question is associate with n_relevant_passages and M-n irrelevant ones.
        Also tokenizes input strings

        N - number of questions in a batch
        M - number of passages per questions
        d - dimension of the model/embeddings

        Returns (a dict of)
        -------------------
        question_inputs: dict[torch.LongTensor]
            input_ids: torch.LongTensor
                shape (N, L)
            **kwargs: more tensors depending on the tokenizer, e.g. attention_mask
        context_inputs: dict[torch.LongTensor]
            input_ids: torch.LongTensor
                shape (N*M, L)
                The first N rows correspond to the relevant contexts for the N questions
                The rest N*(M-1) rows are irrelevant contexts for all questions.
            **kwargs: idem
        """
        # OK (device_ids == local_rank)
        # logger.debug(f"local_rank: {self.args.local_rank}, device_ids: {self.model_wrapped.device_ids}")
        n_irrelevant_passages = self.M-self.n_relevant_passages
        questions, relevant_passages, irrelevant_passages, labels = [], [], [], []
        for i, item in enumerate(items):
            relevant_passage, irrelevant_passage = self.get_training_passages(item)
            if len(relevant_passage) < 1:
                relevant_passage = ['']
                labels.append(self.loss_fct.ignore_index)
            else:
                labels.append(i)
            if len(irrelevant_passage) < n_irrelevant_passages:
                irrelevant_passage.extend(['']*(n_irrelevant_passages-len(irrelevant_passage)))
            questions.append(item['input'])
            relevant_passages.extend(relevant_passage)
            irrelevant_passages.extend(irrelevant_passage)

        question_inputs = self.tokenizer(questions, **self.tokenization_kwargs)
        context_inputs = self.tokenizer(relevant_passages + irrelevant_passages, **self.tokenization_kwargs)
        labels = torch.tensor(labels)
        batch = dict(question_inputs=question_inputs, context_inputs=context_inputs, labels=labels)
        # print(f"collate_fn - local_rank: {self.args.local_rank}\n{batch}")
        return batch

    def compute_loss(self, model, inputs, return_outputs=False):
        """
        Calculates In-batch negatives schema loss and supports to run it in DDP mode by exchanging the representations across all the nodes.
        Adapted from https://github.com/facebookresearch/DPR/blob/main/train_dense_encoder.py

        N. B. this means that the whole representations of questions and contexts, and their similarity matrix, must fit on a single GPU.
        """
        if self.label_smoother is not None:
            raise NotImplementedError()

        local_labels = inputs.pop('labels', None)  # (N, )

        outputs = model(**inputs)

        # Save past state if it exists
        # TODO: this needs to be fixed and made cleaner later.
        if self.args.past_index >= 0:
            self._past = outputs[self.args.past_index]

        if local_labels is None:
            # FIXME: this returns representations and not similarities
            return (None, outputs) if return_outputs else None

        local_question_representations = outputs.question_pooler_output  # (N, d)
        local_context_representations = outputs.context_pooler_output  # (N*M, d)
        if self.args.world_size > 1:
            # copies local representations (in DPR they are moved to CPU but I got a RuntimeError: "Tensors must be CUDA")
            question_representations_to_send = torch.empty_like(local_question_representations).copy_(local_question_representations).detach_()
            context_representations_to_send = torch.empty_like(local_context_representations).copy_(local_context_representations).detach_()
            labels_to_send = torch.empty_like(local_labels).copy_(local_labels)

            # gathers representations from other GPUs
            question_representations_gatherer = [torch.empty_like(question_representations_to_send) for _ in range(self.args.world_size)]
            context_representations_gatherer = [torch.empty_like(context_representations_to_send) for _ in range(self.args.world_size)]
            labels_gatherer = [torch.empty_like(labels_to_send) for _ in range(self.args.world_size)]
            dist.all_gather(question_representations_gatherer, question_representations_to_send)
            dist.all_gather(context_representations_gatherer, context_representations_to_send)
            dist.all_gather(labels_gatherer, labels_to_send)
            
            # keep local vector in the local_rank index (taken from DPR, to not loose the gradients?)
            label_shift = 0
            global_question_representations, global_context_representations, global_labels = [], [], []
            gatherers = zip(question_representations_gatherer, context_representations_gatherer, labels_gatherer)
            for i, (received_question_representations, received_context_representations, received_labels) in enumerate(gatherers):
                # receiving representations from other GPUs
                if i != self.args.local_rank:
                    global_question_representations.append(received_question_representations.to(local_question_representations.device))
                    global_context_representations.append(received_context_representations.to(local_context_representations.device))
                    # labels are defined at the batch-level so we need to shift them when concatening batches
                    received_labels[received_labels!=self.loss_fct.ignore_index] += label_shift
                    label_shift += received_context_representations.shape[0]  # N*M
                    global_labels.append(received_labels.to(local_labels.device))
                # keep local representation
                else:
                    global_question_representations.append(local_question_representations)
                    global_context_representations.append(local_context_representations)
                    # labels are defined at the batch-level so we need to shift them when concatening batches
                    local_labels[local_labels!=self.loss_fct.ignore_index] += label_shift
                    label_shift += local_context_representations.shape[0]  # N*M
                    global_labels.append(local_labels)
            global_question_representations = torch.cat(global_question_representations, dim=0)
            global_context_representations = torch.cat(global_context_representations, dim=0)
            global_labels = torch.cat(global_labels, dim=0)
        else:
            global_question_representations = local_question_representations  # (N, d)
            global_context_representations = local_context_representations  # (N*M, d)
            global_labels = local_labels  # (N, )

        # compute similarity
        similarities = global_question_representations @ global_context_representations.T  # (N, N*M)
        log_probs = self.log_softmax(similarities)

        loss = self.loss_fct(log_probs, global_labels)

        # beware of https://github.com/huggingface/transformers/blob/master/src/transformers/trainer.py#L2513 !!
        # do NOT return log_probs outside of a dict else it will get truncated
        return (loss, dict(log_probs=log_probs)) if return_outputs else loss


class MultiPassageBERTTrainer(QuestionAnsweringTrainer):
    """
    Specific for RC, more precisely MultiPassageBERT
    (will I manage to code an extra-level of abstraction, e.g. ReadingComprehensionTrainer?)

    Parameters
    ----------
    *args, **kwargs: additional arguments are passed to QuestionAnsweringTrainer
    max_n_answers: int, optional
        The answer might be found several time in the same passage, this is a threshold to enable batching
        Defaults to 10.
    ignore_keys: List[str], optional
        List of keys to remove from the batch before feeding it to the model
        (data not used by the model but necessary for evaluation)
        Defaults to ['answer_strings']
    train_original_answer_only: bool, optional
        Whether the model should be trained to predict only the original answer (default)
        or all alternative answers (with the only limit of max_n_answers)
        This has no effect on the evaluation (where all alternative answers are always considered)
    """
    def __init__(self, *args, max_n_answers=10, ignore_keys=['answer_strings'], train_original_answer_only=True, oracle=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.max_n_answers = max_n_answers
        self.ignore_keys = ignore_keys
        self.train_original_answer_only = train_original_answer_only
        self.oracle = oracle
        if self.oracle:
            self.prediction_file_name = "oracle_predictions.json"
            self.metrics_file_name = "oracle_metrics.json"
            if self.n_relevant_passages != self.M:
                warnings.warn(f"Oracle mode. Setting n_relevant_passages={self.M}")
                self.n_relevant_passages = self.M

        # FIXME isn't there a more robust way of defining data_collator as the method collate_fn ?
        self.data_collator = self.collate_fn

    def get_eval_passages(self, item):
        """Keep the top-M passages retrieved by the IR"""
        indices = item[self.search_key+"_indices"][: self.M]
        scores = item[self.search_key+"_scores"][: self.M]
        return self.kb.select(indices)['passage'], scores

    def get_answer_position(self, batch, answers, answer_mask):
        """Adapted from DPR"""
        start_positions, end_positions = torch.zeros_like(answer_mask), torch.zeros_like(answer_mask)
        for j, (input_ids, answer) in enumerate(zip(batch['input_ids'], answers)):
            L = input_ids.size(-1)
            answer_starts, answer_ends = [], []
            for a in answer:
                answer_len = a.size(0)
                enough = False
                for i in range(L-answer_len+1):
                    if (a == input_ids[i: i+answer_len]).all():
                        start, end = i, i+answer_len-1
                        if start not in answer_starts and end not in answer_ends:
                            answer_starts.append(start)
                            answer_ends.append(end)
                            if len(answer_starts) >= self.max_n_answers:
                                enough = True
                                break
                if enough:
                    break
            for i, (start, end) in enumerate(zip(answer_starts, answer_ends)):
                start_positions[j, i] = start
                end_positions[j, i] = end
                # un-mask answer
                answer_mask[j, i] = 1
        start_positions = start_positions.view(-1, self.M, self.max_n_answers)
        end_positions = end_positions.view(-1, self.M, self.max_n_answers)
        answer_mask = answer_mask.view(-1, self.M, self.max_n_answers)
        batch.update(dict(start_positions=start_positions, end_positions=end_positions, answer_mask=answer_mask))
        return batch

    def collate_fn(self, items):
        """
        Collate batch so that each question is associate with n_relevant_passages and M-n irrelevant ones.
        Also tokenizes input strings

        Returns (a dict of)
        -------------------
        input_ids: Tensor[int]
            shape (N * M, L)
        start_positions, end_positions: Tensor[int]
            shape (N, M, max_n_answers)
        answer_mask: Tensor[int]
            shape (N, M, max_n_answers)
        passage_scores: Tensor[float], optional
            shape (N * M)
            only in evaluation mode
        **kwargs: more tensors depending on the tokenizer, e.g. attention_mask
        """
        questions, passages = [], []
        answers, answer_strings = [], []
        passage_scores = []
        N = len(items)
        answer_mask = torch.zeros((N*self.M, self.max_n_answers), dtype=torch.long)
        for i, item in enumerate(items):
            # N. B. seed is set in Trainer
            questions.extend([item['input']]*self.M)

            # oracle -> use only relevant passages
            if (self.args.do_eval or self.args.do_predict) and not self.oracle:
                passage, score = self.get_eval_passages(item)
                passage_scores.extend(score)
                if len(score) < self.M:
                    passage_scores.extend([0]*(self.M-len(score)))
            else:
                relevant_passage, irrelevant_passage = self.get_training_passages(item)
                passage = relevant_passage + irrelevant_passage

            passages.extend(passage)
            # all passages have at least 1 non-masked answer (set to 0 for irrelevant passages)
            answer_mask[i*self.M: i*self.M+len(passage), 0] = 1
            # except for padding passages
            if len(passage) < self.M:
                passages.extend(['']*(self.M-len(passage)))

            original_answer = item['output']['original_answer']
            # avoid processing the same answer twice
            answer = item['output']['answer']
            answer_strings.extend([answer]*self.M)
            # beware this create a discrepancy between answer_strings and answers (tokens)
            # evaluation should always be done using answer_strings
            if self.train_original_answer_only:
                answer = [original_answer]
            else:
                if self.tokenizer.do_lower_case:
                    original_answer = original_answer.lower()
                    answer = list({a.lower() for a in answer} - {original_answer})
                # but ensure the original answer is still the first to be processed
                answer = [original_answer] + answer
            answer = self.tokenizer(answer,
                                    add_special_tokens=False,
                                    return_token_type_ids=False,
                                    return_attention_mask=False)['input_ids']
            answer = [torch.tensor(a, dtype=torch.long) for a in answer]
            answers.extend([answer]*self.M)
        batch = self.tokenizer(*(questions, passages), **self.tokenization_kwargs)
        batch = self.get_answer_position(batch, answers, answer_mask)
        batch['answer_strings'] = answer_strings
        if passage_scores:
            batch['passage_scores'] = torch.tensor(passage_scores)

        return batch

    def _prepare_inputs(self, inputs: dict) -> dict:
        """remove all keys not used by the model but necessary for evaluation before returning Trainer._prepare_inputs"""
        for k in self.ignore_keys:
            if k not in inputs:
                warnings.warn(f"Didn't find {k} in inputs")
                continue
            inputs.pop(k)
        return super()._prepare_inputs(inputs)

    def log_probs_to_answers(self, predictions, input_ids, **kwargs):
        """""
        1. get span start and end positions from log-probabilities
        2. extract actual tokens (answer) from input_ids
        """
        _, _, start_log_probs, end_log_probs = predictions
        passage_indices, start_indices, end_indices = get_best_spans(start_probs=np.exp(start_log_probs),
                                                                     end_probs=np.exp(end_log_probs),
                                                                     **kwargs)
        answers = []
        for i, (passage_index, start, end) in enumerate(zip(passage_indices, start_indices, end_indices)):
            answers.append(input_ids[i, passage_index, start: end])
        return self.tokenizer.batch_decode(answers, skip_special_tokens=True)

    def evaluation_loop(
        self,
        dataloader,
        description: str,
        prediction_loss_only: bool = None,
        ignore_keys: list = None,
        metric_key_prefix: str = "eval",
    ) -> EvalLoopOutput:
        """
        Same as Trainer.evaluation_loop but does not truncate output to the size of the dataset because
        there is M passages per question so the output is M times the size of the dataset

        Also gather input_ids instead of labels in order to recover the tokens from the model's span start and end probabilities
        """
        prediction_loss_only = (
            prediction_loss_only if prediction_loss_only is not None else self.args.prediction_loss_only
        )

        # if eval is called w/o train init deepspeed here
        if self.args.deepspeed and not self.deepspeed:

            # XXX: eval doesn't have `resume_from_checkpoint` arg but we should be able to do eval
            # from the checkpoint eventually
            deepspeed_engine, _, _ = deepspeed_init(self, num_training_steps=0, resume_from_checkpoint=None)
            self.model = deepspeed_engine.module
            self.model_wrapped = deepspeed_engine
            self.deepspeed = deepspeed_engine
            # XXX: we don't need optim/sched for inference, but this needs to be sorted out, since
            # for example the Z3-optimizer is a must for zero3 to work even for inference - what we
            # don't need is the deepspeed basic optimizer which is self.optimizer.optimizer
            deepspeed_engine.optimizer.optimizer = None
            deepspeed_engine.lr_scheduler = None

        model = self._wrap_model(self.model, training=False)

        # if full fp16 is wanted on eval and this ``evaluation`` or ``predict`` isn't called while
        # ``train`` is running, halve it first and then put on device
        if not self.is_in_train and self.args.fp16_full_eval:
            model = model.half().to(self.args.device)

        batch_size = dataloader.batch_size

        print(f"***** Running {description} *****")
        if isinstance(dataloader.dataset, collections.abc.Sized):
            print(f"  Num examples = {self.num_examples(dataloader)}")
        else:
            print("  Num examples: Unknown")
        print(f"  Batch size = {batch_size}")

        model.eval()

        self.callback_handler.eval_dataloader = dataloader
        # Do this before wrapping.
        eval_dataset = dataloader.dataset

        if is_torch_tpu_available():
            dataloader = pl.ParallelLoader(dataloader, [self.args.device]).per_device_loader(self.args.device)

        if self.args.past_index >= 0:
            self._past = None

        # Initialize containers
        # losses/preds/input_ids on GPU/TPU (accumulated for eval_accumulation_steps)
        losses_host = None
        preds_host = None
        input_ids_host = None
        passage_scores_host = None
        # losses/preds/input_ids on CPU (final containers)
        all_losses = None
        all_preds = None
        all_input_ids = None
        all_passage_scores = None
        all_answers = []

        # Will be useful when we have an iterable dataset so don't know its length.
        observed_num_examples = 0

        # Main evaluation loop
        for step, inputs in enumerate(dataloader):
            answer_strings = inputs.get('answer_strings')
            if answer_strings is not None:
                all_answers.extend(answer_strings)
            passage_score = inputs.get('passage_scores')
            if passage_score is not None:
                passage_scores_host = passage_score if passage_scores_host is None else torch.cat((passage_scores_host, passage_score), dim=0)

            # Update the observed num examples
            observed_batch_size = find_batch_size(inputs)
            if observed_batch_size is not None:
                observed_num_examples += observed_batch_size

            # Prediction step
            loss, logits, _ = self.prediction_step(model, inputs, prediction_loss_only, ignore_keys=ignore_keys)

            # Update containers on host
            if loss is not None:
                losses = self._nested_gather(loss.repeat(batch_size))
                losses_host = losses if losses_host is None else torch.cat((losses_host, losses), dim=0)
            if logits is not None:
                logits = self._pad_across_processes(logits)
                logits = self._nested_gather(logits)
                preds_host = logits if preds_host is None else nested_concat(preds_host, logits, padding_index=-100)
            input_ids = self._pad_across_processes(inputs['input_ids'])
            input_ids = self._nested_gather(input_ids)
            input_ids_host = input_ids if input_ids_host is None else nested_concat(input_ids_host, input_ids, padding_index=-100)
            self.control = self.callback_handler.on_prediction_step(self.args, self.state, self.control)

            # Gather all tensors and put them back on the CPU if we have done enough accumulation steps.
            if self.args.eval_accumulation_steps is not None and (step + 1) % self.args.eval_accumulation_steps == 0:
                if losses_host is not None:
                    losses = nested_numpify(losses_host)
                    all_losses = losses if all_losses is None else np.concatenate((all_losses, losses), axis=0)
                if preds_host is not None:
                    logits = nested_numpify(preds_host)
                    all_preds = logits if all_preds is None else nested_concat(all_preds, logits, padding_index=-100)
                input_ids = nested_numpify(input_ids_host)
                all_input_ids = (
                    input_ids if all_input_ids is None else nested_concat(all_input_ids, input_ids, padding_index=-100)
                )
                if passage_scores_host is not None:
                    passage_scores = nested_numpify(passage_scores_host)
                    all_passage_scores = passage_scores if all_passage_scores is None else nested_concat(all_passage_scores, passage_scores, padding_index=0)

                # Set back to None to begin a new accumulation
                losses_host, preds_host, input_ids_host, passage_scores_host = None, None, None, None

        if self.args.past_index and hasattr(self, "_past"):
            # Clean the state at the end of the evaluation loop
            delattr(self, "_past")

        # Number of samples
        if not isinstance(eval_dataset, IterableDataset):
            num_samples = len(eval_dataset)
        elif isinstance(eval_dataset, IterableDatasetShard):
            num_samples = eval_dataset.num_examples
        else:
            num_samples = observed_num_examples

        # Gather all remaining tensors and put them back on the CPU
        if losses_host is not None:
            losses = nested_numpify(losses_host)
            all_losses = losses if all_losses is None else np.concatenate((all_losses, losses), axis=0)
        if preds_host is not None:
            logits = nested_numpify(preds_host)
            all_preds = logits if all_preds is None else nested_concat(all_preds, logits, padding_index=-100)
        if input_ids_host is not None:
            input_ids = nested_numpify(input_ids_host)
            all_input_ids = input_ids if all_input_ids is None else nested_concat(all_input_ids, input_ids, padding_index=-100)
        if passage_scores_host is not None:
            passage_scores = nested_numpify(passage_scores_host)
            all_passage_scores = passage_scores if all_passage_scores is None else nested_concat(all_passage_scores, passage_scores, padding_index=0)

        # reshape like (N, M, L) to ease further processing
        if all_preds is not None:
            all_preds = tuple(pred.reshape(num_samples, self.M, -1) for pred in all_preds)
        if all_input_ids is not None:
            all_input_ids = all_input_ids.reshape(num_samples, self.M, -1)
        if all_passage_scores is not None:
            all_passage_scores = all_passage_scores.reshape(num_samples, self.M)
        if all_answers:
            all_answers = [all_answers[i] for i in range(0, len(all_answers), self.M)]
            assert len(all_answers) == num_samples

        # Metrics!
        if self.compute_metrics is not None and all_preds is not None and all_input_ids is not None and all_answers:
            # 1. raw predictions from scores spans
            predictions = self.log_probs_to_answers(all_preds, all_input_ids)
            predictions, references = format_predictions_for_squad(predictions, all_answers)
            metrics = self.compute_metrics(predictions=predictions, references=references)
            # 2. weighted predictions
            if all_passage_scores is not None:
                weighted_predictions = self.log_probs_to_answers(all_preds, all_input_ids, weights=all_passage_scores)
                weighted_predictions, references = format_predictions_for_squad(weighted_predictions, all_answers)
                for k, v in self.compute_metrics(predictions=weighted_predictions, references=references).items():
                    metrics['weighted_'+k] = v
        else:
            metrics = {}
            predictions = all_preds

        # To be JSON-serializable, we need to remove numpy types or zero-d tensors
        metrics = denumpify_detensorize(metrics)

        if all_losses is not None:
            metrics[f"{metric_key_prefix}_loss"] = all_losses.mean().item()

        # Prefix all keys with metric_key_prefix + '_'
        for key in list(metrics.keys()):
            if not key.startswith(f"{metric_key_prefix}_"):
                metrics[f"{metric_key_prefix}_{key}"] = metrics.pop(key)

        return EvalLoopOutput(predictions=predictions, label_ids=None, metrics=metrics, num_samples=num_samples)

    

        
class BERTRankerTrainer(QuestionAnsweringTrainer):
    """
    Specific for RC, more precisely BERTRanker
    
    Parameters
    ----------
    *args, **kwargs: additional arguments are passed to QuestionAnsweringTrainer
    """
    def __init__(self, *args, oracle=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.cls = -1
        self.oracle = oracle
        if self.oracle:
            self.prediction_file_name = "oracle_predictions.json"
            self.metrics_file_name = "oracle_metrics.json"
            if self.n_relevant_passages != self.M:
                warnings.warn(f"Oracle mode. Setting n_relevant_passages={self.M}")
                self.n_relevant_passages = self.M
        assert self.n_relevant_passages == 1
        # FIXME isn't there a more robust way of defining data_collator as the method collate_fn ?
        self.data_collator = self.collate_fn
    
    def get_eval_passages(self, item):
        """Keep the top-M passages retrieved by the IR"""
        indices  = item[self.search_key+"_indices"][: self.M]
        scores   = item[self.search_key+"_scores"][: self.M]
        relevants = item[self.search_key+"_provenance_indices"] + item[self.search_key+"_alternative_indices"]
        original = item[self.search_key+"_provenance_indices"]
        mask     = np.in1d(indices, original)
        label    = np.where(mask == True)[0][0] if len(np.where(mask == True)[0]) > 0 else -100
        return self.kb.select(indices)['passage'], scores, indices, relevants, label

    def write_predictions(self, predictions, resume_from_checkpoint):
        file_path = str(resume_from_checkpoint)
        file_path = '/'.join(file_path.split('/')[:-1])
        file_path  = Path(file_path)/"predictions.pt"
        torch.save(predictions, file_path)
    
    
    def write_metrics(self, metrics, resume_from_checkpoint):
        print(metrics)
        file_path = str(resume_from_checkpoint)
        file_path = '/'.join(file_path.split('/')[:-1])
        file_path  = Path(file_path)/self.metrics_file_name
        with open(file_path, "w") as file:
            json.dump(metrics, file)
    
    
    def collate_fn(self, items):
        """
        Collate batch so that each question is associate with n_relevant_passages and M-n irrelevant ones.
        Also tokenizes input strings

        Returns (a dict of)
        -------------------
        input_ids: Tensor[int]
            shape (N * M, L)
        passage_scores: Tensor[float], optional
            shape (N * M)
            only in evaluation mode
        **kwargs: more tensors depending on the tokenizer, e.g. attention_mask
        """
        questions, passages, switch_labels = [], [], []
        passage_scores = []
        indices, relevants = [], []
        N = len(items)
        self.cls += 1
        for i, item in enumerate(items):
            # N. B. seed is set in Trainer
            questions.extend([item['input']]*self.M)

            # oracle -> use only relevant passages
            if (self.args.do_eval or self.args.do_predict) and not self.oracle:
                passage, score, index, relevant, label = self.get_eval_passages(item)
                passage_scores.extend(score)
                indices.append(index)
                relevants.append(relevant+[-1]*(1000-len(relevant)))
                switch_labels.append(label)
                
                if len(score) < self.M:
                    passage_scores.extend([0]*(self.M-len(score)))
            else:
                relevant_passage, irrelevant_passage = self.get_training_passages(item)
                #if there is no relevant passage set label = -100, so it will be ignore when computing the loss
                if len(relevant_passage) == 0:
                    switch_labels.append(-100)
                else: switch_labels.append(0)
                passage = relevant_passage + irrelevant_passage

            passages.extend(passage)
            
            # padding passages
            if len(passage) < self.M:
                passages.extend(['']*(self.M-len(passage)))
            
        batch = self.tokenizer(*(questions, passages), **self.tokenization_kwargs)
        batch['N'] = N
        batch['M'] = self.M
        batch['cls'] = self.cls
        batch['switch_labels'] = torch.tensor(switch_labels)
        
        if indices:
            batch['indices']   = torch.tensor(indices)
            batch['relevants'] = torch.tensor(relevants)
        
        return batch
        

    def _prepare_inputs(self, inputs: dict) -> dict:
        """remove all keys not used by the model but necessary for evaluation before returning Trainer._prepare_inputs"""
        
        return super()._prepare_inputs(inputs)
    


        
class ViLTRankerTrainer(QuestionAnsweringTrainer):
    """
    Specific for RC, more precisely BERTRanker
    
    Parameters
    ----------
    *args, **kwargs: additional arguments are passed to QuestionAnsweringTrainer
    """
    def __init__(self, *args, feature_extractor=None,
                 passage2image_file_name="", image_dir="", oracle=False, **kwargs):
    
        super().__init__(*args, **kwargs)
        
        self.oracle = oracle
        if self.oracle:
            self.prediction_file_name = "oracle_predictions.json"
            self.metrics_file_name = "oracle_metrics.json"
            if self.n_relevant_passages != self.M:
                warnings.warn(f"Oracle mode. Setting n_relevant_passages={self.M}")
                self.n_relevant_passages = self.M
        assert self.n_relevant_passages == 1
        
        self.passage2image = json_load(passage2image_file_name)
        self.image_dir = image_dir
        self.feature_extractor = feature_extractor
        
        # FIXME isn't there a more robust way of defining data_collator as the method collate_fn ?
        self.data_collator = self.collate_fn
    
    
    def get_eval_passages(self, item):
        #print("GET Evaluation Passages")
        """Keep the top-M passages retrieved by the IR"""
        indices = item[self.search_key+"_indices"][: self.M]
        images  = [Path(self.image_dir) / self.passage2image[str(index)] for index in indices]
        scores  = item[self.search_key+"_scores"][: self.M]
        
        relevants = item[self.search_key+"_provenance_indices"] + item[self.search_key+"_alternative_indices"]
        original  = item[self.search_key+"_provenance_indices"]
        
        mask  = np.in1d(indices, original)
        label = np.where(mask == True)[0][0] if len(np.where(mask == True)[0]) > 0 else -100
        return self.kb.select(indices)['passage'], images, scores, indices, relevants, label

    
    def get_training_passages(self, item):
        relevant_passages = []
        relevant_images   = []
        all_relevant_indices = item[self.search_key+"_provenance_indices"]
        n_relevant = min(len(all_relevant_indices), self.n_relevant_passages)
        if n_relevant > 0:
            relevant_indices = np.random.choice(all_relevant_indices, n_relevant, replace=False)
            if len(relevant_indices) > 0:
                relevant_passages = self.kb.select(relevant_indices)['passage']
                relevant_images   = [Path(self.image_dir) / self.passage2image[str(index)] for index in relevant_indices]
        irrelevant_passages = []
        all_irrelevant_indices = item[self.search_key+"_irrelevant_indices"]
        n_irrelevant = min(len(all_irrelevant_indices), self.M-self.n_relevant_passages)
        if n_irrelevant > 0:
            irrelevant_indices = np.random.choice(all_irrelevant_indices, n_irrelevant, replace=False)
            if len(irrelevant_indices) > 0:
                irrelevant_passages = self.kb.select(irrelevant_indices)['passage']
                irrelevant_images   = [Path(self.image_dir) / self.passage2image[str(index)] for index in irrelevant_indices]
        elif n_relevant <= 0:
            warnings.warn(f"Didn't find any passage for question {item['id']}")
        return relevant_passages, irrelevant_passages, relevant_images, irrelevant_images
    

    def write_predictions(self, predictions, resume_from_checkpoint):
        file_path = str(resume_from_checkpoint)
        file_path = '/'.join(file_path.split('/')[:-1])
        file_path  = Path(file_path)/"predictions.pt"
        torch.save(predictions, file_path)
    
    
    def write_metrics(self, metrics, resume_from_checkpoint):
        print(metrics)
        file_path = str(resume_from_checkpoint)
        file_path = '/'.join(file_path.split('/')[:-1])
        file_path  = Path(file_path)/self.metrics_file_name
        with open(file_path, "w") as file:
            json.dump(metrics, file)
    
    
    def collate_fn(self, items):
        """
        Collate batch so that each question is associate with n_relevant_passages and M-n irrelevant ones.
        Also tokenizes input strings

        Returns (a dict of)
        -------------------
        input_ids: Tensor[int]
            shape (N * M, L)
        passage_scores: Tensor[float], optional
            shape (N * M)
            only in evaluation mode
        **kwargs: more tensors depending on the tokenizer, e.g. attention_mask
        """
        questions, passages, switch_labels = [], [], []
        question_imgs, passage_imgs = [], []
        passage_scores = []
        indices, relevants = [], []
        N = len(items)
        for i, item in enumerate(items):
            # N. B. seed is set in Trainer
            questions.extend([item['input']]*self.M)
            question_imgs.extend([Path(self.image_dir) / item['image']]*self.M)            
            
            # oracle -> use only relevant passages
            if (self.args.do_eval or self.args.do_predict) and not self.oracle:
                passage, image, score, index, relevant, label = self.get_eval_passages(item)
                #passage_imgs.extend(image)
                passage_scores.extend(score)
                indices.append(index)
                relevants.append(relevant+[-1]*(1000-len(relevant)))
                switch_labels.append(label)
                
                if len(score) < self.M:
                    passage_scores.extend([0]*(self.M-len(score)))
            else:
                relevant_passage, irrelevant_passage, relevant_image, irrelevant_image = self.get_training_passages(item)
                #if there is no relevant passage set label = -100, so it will be ignore when computing the loss
                if len(relevant_passage) == 0:
                    switch_labels.append(-100)
                else: switch_labels.append(0)
                passage = relevant_passage + irrelevant_passage
                image   = relevant_image   + irrelevant_image

            passages.extend(passage)
            passage_imgs.extend(image)
            
            # padding passages
            if len(passage) < self.M:
                passages.extend(['']*(self.M-len(passage)))
                passage_imgs.extend(['']*(self.M-len(passage)))
            
        batch = self.tokenizer(*(questions, passages), **self.tokenization_kwargs)
        batch = self.get_visual_embeddings(batch, question_imgs, passage_imgs)
        batch['N'] = N
        batch['M'] = self.M
        batch['switch_labels'] = torch.tensor(switch_labels)
        if indices:
            batch['indices']   = torch.tensor(indices)
            batch['relevants'] = torch.tensor(relevants)
        
        return batch
    
    def _is_resizable(self, image, size=384, size_divisor=32):
        shorter=size
        longer = int((1333 / 800) * shorter)

        w, h = image.size
        min_size = shorter
        max_size = longer
        scale = min_size / min(w, h)

        if h < w:
            newh, neww = min_size, scale * w
        else:
            newh, neww = scale * h, min_size

        if max(newh, neww) > max_size:
            scale = max_size / max(newh, neww)
            newh = newh * scale
            neww = neww * scale

        newh, neww = int(newh + 0.5), int(neww + 0.5)
        newh, neww = newh // size_divisor * size_divisor, neww // size_divisor * size_divisor

        return not(newh==0 or neww==0)

    def _get_image_pixels(self, img_path):
        
        img_path = str(img_path)
        size = (self.model.config.image_size, int(1333 / 800 * self.model.config.image_size + 0.5))
        
        if img_path == '':
            img = Image.new('RGB', size)
        else:
            #print("img_path: ", img_path)
            img = Image.open(img_path).convert('RGB')
            
            if not(self._is_resizable(img, size=self.model.config.image_size)):
                img = img.resize(size, resample=Image.NEAREST)
            
        return img
        
            
    def get_visual_embeddings(self, inputs, questions, passages):
        
        images = [] 
        
        ## all images
        names = questions + passages
        for name in names:
            images.append(self._get_image_pixels(name))
        
        sizes = [img.size for img in images]
        #print("sizes:", sizes)

        #for l in range(len(sizes)):
        #    if sizes[l][0] == 0 or sizes[l][1] == 0:
        #        print("image index", l)
        #        print("image name", images[i])
        
        encodings = self.feature_extractor(images, **self.tokenization_kwargs)
        
        pixel_values = torch.stack([encodings.pixel_values[:len(questions)], encodings.pixel_values[len(questions):]], dim=1)
        pixel_mask   = torch.stack([encodings.pixel_mask[:len(questions)],   encodings.pixel_mask[len(questions):]], dim=1)
        
        inputs.update(
            {
                "pixel_values": pixel_values,
                "pixel_mask": pixel_mask,
            }
        )
        
        return inputs
        

    def _prepare_inputs(self, inputs: dict) -> dict:
        """remove all keys not used by the model but necessary for evaluation before returning Trainer._prepare_inputs"""
        
        return super()._prepare_inputs(inputs)
    


def get_checkpoint(resume_from_checkpoint: str, *args, **kwargs):
    if args or kwargs:
        warnings.warn(f"ignoring additional arguments:\n{args}\n{kwargs}")
    cpt = Path(resume_from_checkpoint)
    # weird trick to glob using pathlib
    resume_from_checkpoints = list(cpt.parent.glob(cpt.name))
    return resume_from_checkpoints


def instantiate_trainer(trainee, trainer_class="MultiPassageBERTTrainer", debug=False, 
                        train_dataset=None, eval_dataset=None, metric='squad', 
                        training_kwargs={}, callbacks_args=[], **kwargs):
    """Additional arguments are passed to Trainer"""
    # debug (see torch.autograd.detect_anomaly)
    set_detect_anomaly(debug)

    # data
    if train_dataset is not None:
        train_dataset = load_from_disk(train_dataset)#.shard(num_shards=100, index=0)
    if eval_dataset is not None:
        eval_dataset = load_from_disk(eval_dataset)#.shard(num_shards=100, index=0)

    # training
    # revert the post-init that overrides do_eval
    do_eval = training_kwargs.pop('do_eval', False)
    training_args = TrainingArguments(**training_kwargs)
    training_args.do_eval = do_eval

    # metrics come in priority from meerqat.train.metrics
    if metric is not None:
        compute_metrics = getattr(metric_functions, metric, None)
        # or from HF's datasets
        if compute_metrics is None:
            metric = load_metric(metric)
            compute_metrics = metric.compute
    else:
        compute_metrics = None

    TrainerClass = getattr(sys.modules[__name__], trainer_class)
    trainer = TrainerClass(model=trainee, args=training_args,
                           train_dataset=train_dataset, eval_dataset=eval_dataset,
                           compute_metrics=compute_metrics, **kwargs)
    # training callbacks
    for callback in callbacks_args:
        CallbackClass = getattr(trainer_callback, callback.pop("Class"))
        trainer.add_callback(CallbackClass(**callback))

    return trainer, training_args


if __name__ == "__main__":
    logger.debug(f"entering main {max_memory_usage(human=True)}")
    # load and parse arguments
    args = docopt(__doc__)
    config_path = Path(args['<config>'])
    with open(config_path, "r") as file:
        config = load_pretrained_in_kwargs(json.load(file))

    logger.debug(f"after loading pre-trained models {max_memory_usage(human=True)}")

    verbosity = config.pop("verbosity", None)
    if verbosity is not None:
        t_logging.set_verbosity(verbosity)
        logger.setLevel(verbosity)

    checkpoint = config.pop("checkpoint", {})
    trainer, training_args = instantiate_trainer(**config)
    device = trainer.args.device
    logger.debug(f"after instantiating trainer {max_memory_usage(human=True)}")
    if training_args.do_train:
        trainer.train(**checkpoint)
    elif training_args.do_eval:
        resume_from_checkpoints = get_checkpoint(**checkpoint)
        for resume_from_checkpoint in tqdm(resume_from_checkpoints, desc="Evaluation"):
            # load state dict
            state_dict_path = resume_from_checkpoint / WEIGHTS_NAME
            if not state_dict_path.exists():
                continue
            state_dict = torch.load(state_dict_path, map_location=device)
            trainer._load_state_dict_in_model(state_dict)

            # optionally load trainer state for better logging
            trainer_state = resume_from_checkpoint/"trainer_state.json"
            if trainer_state.is_file():
                trainer.state = TrainerState.load_from_json(trainer_state)
            else:
                warnings.warn("couldn't load trainer state, TB logging might use an inappropriate step")
            metrics = trainer.evaluate()
            trainer.write_metrics(metrics, resume_from_checkpoint)
    elif training_args.do_predict:
        resume_from_checkpoints = get_checkpoint(**checkpoint)
        for resume_from_checkpoint in tqdm(resume_from_checkpoints, desc="Prediction"):
            # load state dict
            state_dict_path = resume_from_checkpoint / WEIGHTS_NAME
            if not state_dict_path.exists():
                continue
            state_dict = torch.load(state_dict_path, map_location=device)
            trainer._load_state_dict_in_model(state_dict)

            # run model on evaluation dataset
            prediction_output = trainer.predict(trainer.eval_dataset)
            trainer.write_metrics(prediction_output.metrics, resume_from_checkpoint)
            trainer.write_predictions(prediction_output.predictions, resume_from_checkpoint)
    else:
        warnings.warn("Did nothing except instantiate the trainer, "
                      "you probably want to set do_train, do_eval or do_predict to True"
                      f"see {training_args.__doc__}")
