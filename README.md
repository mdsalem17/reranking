# Reranking Methods
In this repository contains information on how to download the dataset, the knowledge base, and how to replicate the results. 
To use [Reranking Transformers](https://github.com/mdsalem17/RerankingTransformer) on ViQuAE.

Note that the prediction scores of the rerankers are stored in the folder `rerankers/`.

# ViQuAE
Source code and data used in the paper [*ViQuAE, a Dataset for Knowledge-based Visual Question Answering about Named Entities*](https://hal.archives-ouvertes.fr/hal-03650618), Lerner et al., SIGIR'22. 

See also [MEERQAT project](https://www.meerqat.fr/).


# Getting the dataset and KB

The data is provided in two formats: HF's `datasets` (based on Apache Arrow) and plain-text JSONL files (one JSON object per line). 
Both formats can be used in the same way as `datasets` parses objects into python `dict` (see below), however our code only supports (and is heavily based upon) `datasets`.
Images are distributed separately, in standard formats (e.g. jpg).
Both dataset formats are distributed in two versions, with (TODO) and without pre-computed features.
The pre-computed feature version allows you to skip one or several step described in [EXPERIMENTS.md](./EXPERIMENTS.md) (e.g. face detection).

## The images

Here’s how to get the images grounding the questions of the dataset:
```sh
# get the images. TODO integrate this in a single dataset
git clone https://huggingface.co/datasets/PaulLerner/viquae_images
# to get ALL images (dataset+KB) use https://huggingface.co/datasets/PaulLerner/viquae_all_images instead 
cd viquae_images
# in viquae_all_images, the archive is split into parts of 5GB
# cat parts/* > images.tar.gz
tar -xzvf images.tar.gz
export VIQUAE_IMAGES_PATH=$PWD/images
```

Alternatively, you can download images from Wikimedia Commons using `meerqat.data.kilt2vqa download` (see below).

## The ViQuAE dataset

If you don’t want to use `datasets` you can get the data directly from https://huggingface.co/datasets/PaulLerner/viquae_dataset (e.g. `git clone https://huggingface.co/datasets/PaulLerner/viquae_dataset`).

The dataset format largely follows [KILT](https://huggingface.co/datasets/kilt_tasks). 
Here I’ll describe the dataset without pre-computed features. Pre-computed features are basically the output of each step described in [EXPERIMENTS.md](./EXPERIMENTS.md).

```py
In [1]: from datasets import load_dataset
   ...: dataset = load_dataset('PaulLerner/viquae_dataset')
In [2]: dataset
Out[2]: 
DatasetDict({
    train: Dataset({
        features: ['image', 'input', 'kilt_id', 'id', 'meta', 'original_question', 'output', 'url', 'wikidata_id'],
        num_rows: 1190
    })
    validation: Dataset({
        features: ['image', 'input', 'kilt_id', 'id', 'meta', 'original_question', 'output', 'url', 'wikidata_id'],
        num_rows: 1250
    })
    test: Dataset({
        features: ['image', 'input', 'kilt_id', 'id', 'meta', 'original_question', 'output', 'url', 'wikidata_id'],
        num_rows: 1257
    })
})
In [3]: item = dataset['test'][0]

# this is now a dict, like the JSON object loaded from the JSONL files
In [4]: type(item)
Out[4]: dict

# url of the grounding image
In [5]: item['url']
Out[5]: 'http://upload.wikimedia.org/wikipedia/commons/thumb/a/ae/Jackie_Wilson.png/512px-Jackie_Wilson.png'

# file name of the grounding image as stored in $VIQUAE_IMAGES_PATH
In [6]: item['image']
Out[6]: '512px-Jackie_Wilson.png'

# you can thus load the image from $VIQUAE_IMAGES_PATH/item['image']
# meerqat.data.loading.load_image_batch does that for you
In [7]: from meerqat.data.loading import load_image_batch
# fake batch of size 1
In [8]: image = load_image_batch([item['image']])[0]
# it returns a PIL Image, all images have been resized to a width of 512
In [9]: type(image), image.size
Out[9]: (PIL.Image.Image, (512, 526))

# question string
In [10]: item['input']
Out[10]: "this singer's re-issued song became the UK Christmas number one after helping to advertise what brand?"

# answer string
In [11]: item['output']['original_answer']
Out[11]: "Levi's"

# processing the data:
In [12]: dataset.map(my_function)
# this is almost the same as (see how can you adapt the code if you don’t want to use the `datasets` library)
In [13]: for item in dataset:
    ...:     my_function(item)
```

## The ViQuAE Knowledge Base (KB)

Again, the format of the KB is very similar to [KILT’s Wikipedia](https://huggingface.co/datasets/kilt_wikipedia) so I will not describe all fields exhaustively.

```py
# again you can also clone directly from https://huggingface.co/datasets/PaulLerner/viquae_wikipedia to get the raw data
>>> data_files = dict(
    humans_with_faces='humans_with_faces.jsonl.gz', 
    humans_without_faces='humans_without_faces.jsonl.gz', 
    non_humans='non_humans.jsonl.gz'
)
>>> kb = load_dataset('PaulLerner/viquae_wikipedia', data_files=data_files)
>>> kb
DatasetDict({
    humans_with_faces: Dataset({
        features: ['anchors', 'categories', 'image', 'kilt_id', 'text', 'url', 'wikidata_info', 'wikipedia_id', 'wikipedia_title'],
        num_rows: 506237
    })
    humans_without_faces: Dataset({
        features: ['anchors', 'categories', 'image', 'kilt_id', 'text', 'url', 'wikidata_info', 'wikipedia_id', 'wikipedia_title'],
        num_rows: 35736
    })
    non_humans: Dataset({
        features: ['anchors', 'categories', 'image', 'kilt_id', 'text', 'url', 'wikidata_info', 'wikipedia_id', 'wikipedia_title'],
        num_rows: 953379
    })
})
>>> item = kb['humans_with_faces'][0]
>>> item['wikidata_info']['wikidata_id'], item['wikidata_info']['wikipedia_title']
('Q313590', 'Alain Connes')
# file name of the reference image as stored in $VIQUAE_IMAGES_PATH
# you can use meerqat.data.loading.load_image_batch like above
>>> item['image']
'512px-Alain_Connes.jpg'
# the text is stored in a list of string, one per paragraph
>>> type(item['text']['paragraph']), len(item['text']['paragraph'])
(list, 25)
>>> item['text']['paragraph'][1]
"Alain Connes (; born 1 April 1947) is a French mathematician, \
currently Professor at the Collège de France, IHÉS, Ohio State University and Vanderbilt University. \
He was an Invited Professor at the Conservatoire national des arts et métiers (2000).\n"
```

To format the articles into text passages, follow instructions at [EXPERIMENTS.md](./EXPERIMENTS.md) (Preprocessing passages section).
Alternatively, get them from https://huggingface.co/datasets/PaulLerner/viquae_passages (`load_dataset('PaulLerner/viquae_passages')`)

# Annotation of the data

Please refer to [`ANNOTATION.md`](./ANNOTATION.md) for the annotation instructions

# Experiments

Please refer to [EXPERIMENTS.md](./EXPERIMENTS.md) for instructions to reproduce our experiments

# Reference

If you use this code or the ViQuAE dataset, please cite our paper:
```
@inproceedings{lerner2022,
   author = {Paul Lerner and Olivier Ferret and Camille Guinaudeau and Le Borgne, Hervé  and Romaric
   Besançon and Moreno, Jose G  and Lovón Melgarejo, Jesús },
   year={2022},
   title={{ViQuAE}, a
   Dataset for Knowledge-based Visual Question Answering about Named
   Entities},
   booktitle = {Proceedings of The 45th International ACM SIGIR Conference on Research and Development in Information Retrieval},
	series = {SIGIR’22},
   URL = {https://hal.archives-ouvertes.fr/hal-03650618},
   DOI = {10.1145/3477495.3531753},
   publisher = {Association for Computing Machinery},
   address = {New York, NY, USA}
}
```

# `meerqat`
This should contain all the source code and act as a python package (e.g. `import meerqat`)

## Installation

Install PyTorch 1.9.0 following [the official document wrt to your distribution](https://pytorch.org/get-started/locally/) (preferably in a virtual environment)

Also install [ElasticSearch](https://www.elastic.co/guide/en/elastic-stack-get-started/current/get-started-elastic-stack.html#install-elasticsearch) 
(and run it) if you want to do sparse retrieval.

The rest should be installed using `pip`:
```sh
git clone https://github.com/PaulLerner/ViQuAE.git
pip install -e ViQuAE
```

## `image`, `ir`, `models`, `train`
Those modules are best described along with the experiments, see [EXPERIMENTS.md](./EXPERIMENTS.md).

## `meerqat.data`

This should contain scripts to load the data, annotate it... 

### `loading`
This is probably the only file in `data` interesting for the users of the dataset. 
The documentation below describes modules that were used to **annotate the dataset**.

#### `passages` 
Segments Wikipedia articles (from the `kilt_wikipedia` dataset) into passages (e.g. paragraphs)
Current options (passed in a JSON file) are:
 - `prepend_title`: whether to prepend the title at the beginning of each passage like `"<title> [SEP] <passage>"`
 - `special_fields`: removes the title, sections titles ("Section::::") and bullet-points ("BULLET::::")
 - `uniform`: each passage is `n` tokens, without overlap. Tokenized with a `transformers` tokenizer
 - `uniform_sents`: each article is first segmented into sentences using `spacy`. 
                    Then sentences are grouped into passage s.t. each passage holds a maximum of `n` tokens 
                    (`spacy` tokens here, not `transformers` like above)

#### `map`
Make a JSON file out of a `dataset` column for quick (and string) indexing.
 
### `kilt2vqa.py`
All the data should be stored in the `data` folder, at the root of this repo.

The goal is to generate questions suitable for VQA by replacing explicit entity mentions in existing textual QA datasets
 by an ambiguous one and illustrate the question with an image (that depicts the entity).

[4 steps](./figures/kilt2vqa_big_picture.png) (click on the links to see the figures):
1. [`ner`](./figures/kilt2vqa_nlp.png) - Slight misnomer, does a bit more than NER, i.e. dependency parsing.  
    Detected entities with valid type and dependency are replaced by a placeholder along with its syntactic children.  
    e.g. 'Who wrote *the opera **Carmen***?' &rarr; 'Who wrote `{mention}`'  
    Note that, not only the entity mention ('Carmen') but its syntactic children ('the opera')
    are replaced by the placeholder.
2. [`ned`](./figures/kilt2vqa_nlp.png) - Disambiguate entity mentions using Wikipedia pages provided in KILT.  
    TriviaQA was originally framed as a reading-comprehension problem so the authors applied off-the-shelf NED and filtered
    out pages that didn't contain the answer.  
    For every entity mention we compute Word Error Rate (WER, i.e. word-level Levenshtein distance) for every wikipedia title
    and aliases. We save the minimal match and WER and recommand filtering out WER > 0.5  
    More data about these entities is gathered in `wiki.py`, 
    just run `kilt2vqa.py count_entities` first to save a dict with all disambiguated entities (outputs `entities.json`).
3. [`generate mentions`](./figures/kilt2vqa_mentiong_gen.png) - Generate ambiguous entity mentions that can be used to replace the placeholder in the input question 
    (you need to run `wiki.py data` first):  
    - if the gender is available (not animal sex):
        - 'this man' or 'this woman' (respecting transgender)
        - 'he/him/his' or 'she/her/hers' w.r.t mention dependency              
    - if human and occupation is available : 'this `{occupation}`' (respecting gender if relevant, e.g. for 'actress')
    - else if non-human:
        - if a taxon : 'this `{taxon rank}`' (e.g. 'species') 
        - else 'this `{class}`' (e.g. 'this tower')        
4.  `generate vq` - make the VQA triple by choosing:  
        - uniformly a mention type and a mention from this mention type (generated in the previous step)  
        - the image with the best score (according to the heuristics computed in `wiki.py commons heuristics`).
          Tries to use a unique image per entity.

`labelstudio` first calls `generate vq` i.e. no need to call both!  
The dataset is then converted to the Label Studio JSON format so you can annotate and convert the errors of the automatic pipeline (see [`ANNOTATION.md`](./ANNOTATION.md)).

`download` downloads images (set in `meerqat.data.wiki data entities`) from Wikimedia Commons using `meerqat.data.wiki.save_image`.  
This might take a while (thus the sharding options), any help/advice is appreciated :)

### `wiki.py`

Gathers data about entities mentioned in questions via Wikidata, Wikimedia Commons SPARQL services and Wikimedia REST API.

You should run all of these in this order to get the whole cake:

#### `wiki.py data entities <subset>` 
**input/output**: `entities.json` (output of `kilt2vqa.py count_entities`)  
queries many different attributes for all entities in the questions 

Also sets a 'reference image' to the entity using Wikidata properties in the following order of preference:
- P18 ‘image’ (it is roughly equivalent to the infobox image in Wikipedia articles)
- P154 ‘logo image’
- P41 ‘flag image’
- P94 ‘coat of arms image’
- P2425 ‘service ribbon image’

#### `wiki.py data feminine <subset>` 
**input**: `entities.json`  
**output**: `feminine_labels.json`  
gets feminine labels for classes and occupations of these entities

#### `wiki.py data superclasses <subset> [--n=<n>]` 
**input**: `entities.json`  
**output**: `<n>_superclasses.json`  
gets the superclasses of the entities classes up `n` level (defaults to 'all', i.e. up to the root)
#### (OPTIONAL) we found that heuristics/images based on depictions were not that discriminative
##### `wiki.py commons sparql depicts <subset>`
**input/output**: `entities.json`  
Find all images in Commons that *depict* the entities
##### `wiki.py commons sparql depicted <subset>`
**input**: `entities.json`  
**output**: `depictions.json`  
Find all entities depicted in the previously gathered step
##### `wiki.py data depicted <subset>` 
**input**: `entities.json`, `depictions.json`   
**output**: `entities.json`  
Gathers the same data as in `wiki.py data entities <subset>` for *all* entities depicted in any of the depictions  
Then apply a heuristic to tell whether an image depicts the entity prominently or not: 
> *the depiction is prominent if the entity is the only one of its class*  
  e.g. *pic of Barack Obama and Joe Biden* -> not prominent  
       *pic of Barack Obama and the Eiffel Tower* -> prominent  

Note this heuristic is not used in `commons heuristics`

#### `wiki.py filter <subset> [--superclass=<level> --positive --negative --deceased=<year> <classes_to_exclude>...]`
**input/output**: `entities.json`  
Filters entities w.r.t. to their class/nature/"instance of" and date of death, see `wiki.py` docstring for option usage (TODO share concrete_entities/abstract_entities)
Also entities with a ‘sex or gender’ (P21) or ‘occupation’ (P106) are kept by default.

Note this deletes data so maybe save it if you're unsure about the filter.

#### `wiki.py commons rest <subset> [--max_images=<max_images> --max_categories=<max_categories>]`
**input/output**: `entities.json`  

Gathers images and subcategories recursively from the entity root commons-category

Except if you have a very small dataset you should probably set `--max_images=0` to query only categories and use `wikidump.py` to gather images from those.  
`--max_categories` defaults to 100.

#### `wiki.py commons heuristics <subset> [<heuristic>...]`
**input/output**: `entities.json`  
Run `wikidump.py` first to gather images.  
Compute heuristics for the image (control with `<heuristic>`, default to all):
- `categories`: the entity label should be included in *all* of the image category
- `description`: the entity label should be included in the image description
- `title`: the entity label should be included in the image title/file name
- `depictions`: the image should be tagged as *depicting* the entity (gathered in `commons sparql depicts`)

### `wikidump.py`
**input/output**: `entities.json`  
Usage: `wikidump.py <subset>`  
Parses the dump (should be downloaded first, TODO add instructions), gathers images and assign them to the relevant entity given its common categories (retrieved in `wiki.py commons rest`)  
Note that the wikicode is parsed very lazily and might need a second run depending on your application, e.g. templates are not expanded...

### `labelstudio`
Used to manipulate the output of [Label Studio](https://labelstud.io/), see also [ANNOTATION.md](./ANNOTATION.md)  
- `assign` takes annotations out of the TODO list in a `tasks.json` file (input to LS)
- `save images` similar to `kilt2vqa download`, not used for the final dataset
- `merge` merges several LS outputs, also compute inter-annotator agreement and saves disagreements
- `agree` merges the output of `merge` along with the corrected disagreements

