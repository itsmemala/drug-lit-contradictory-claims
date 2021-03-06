"""Functions for processing claims extracted from cord-19."""

# -*- coding: utf-8 -*-

from itertools import combinations

# import en_core_sci_lg
import nltk
nltk.download('punkt')
import pandas as pd  # noqa: E402
import spacy  # noqa: E402
from nltk import sent_tokenize  # noqa: E402
from numba import jit  # noqa: E402
# import scispacy  # noqa: F401
from scispacy.abbreviation import AbbreviationDetector  # noqa: E402
from scispacy.umls_linking import UmlsEntityLinker  # noqa: E402
from sklearn.metrics.pairwise import cosine_similarity  # noqa: E402
# from spacy.vocab import Vocab


def initialize_nlp(virus_lex_path: str, scispacy_model_name: str = "en_core_sci_lg"):
    """
    Initialize scispacy nlp object and virus terms to the vocabulary.

    :param virus_lex_path: path to virus lexicon
    :param scispacy_model_name: name of scispacy model to use for w2v vectors
    :return: Scispacy nlp object
    """
    # Load the scispacy large model
    # nlp = en_core_sci_lg.load(disable='parser')
    # I believe this should work, I wonder if it's not recommended for  memory reasons though in a v env like Travis...
    nlp = spacy.load(scispacy_model_name, disable='parser')
    # Enable umls entity detection and abbreviation detection
    linker = UmlsEntityLinker(resolve_abbreviations=True)
    nlp.add_pipe(linker)
    abbreviation_pipe = AbbreviationDetector(nlp)
    nlp.add_pipe(abbreviation_pipe)

    # Create a new vector to assign to the virus terms
    new_vector = nlp("""Positive-sense single‐stranded ribonucleic acid virus, subgenus """
                     """sarbecovirus of the genus Betacoronavirus. """
                     """Also known as severe acute respiratory syndrome coronavirus 2, """
                     """also known by 2019 novel coronavirus. It is """
                     """contagious in humans and is the cause of the ongoing pandemic of """
                     """coronavirus disease. Coronavirus disease 2019 is a zoonotic infectious """
                     """disease.""").vector

    # Add virus terms to the model vocabulary and assign to them the new vector created above
    # vocab = Vocab()
    virus_words = pd.read_csv(virus_lex_path, header=None)
    for virus_word in virus_words[0]:
        nlp.vocab.set_vector(virus_word, new_vector)

    return nlp


def split_papers_on_claim_presence(claims_df: pd.DataFrame):
    """
    Separate papers with at least 1 claim from those with no claims.

    :param claims_df: pandas dataframe of publication text, with a flag indicating claim presence
    :return: Separate dataframes for claim text and text for papers with no claims
    """
    no_claims_cord_uid = set(claims_df.loc[claims_df.claim_flag == 0, 'cord_uid'])\
        - set(claims_df.loc[claims_df.claim_flag == 1, 'cord_uid'])
    claims_data = claims_df.loc[claims_df.claim_flag == 1, :].copy().reset_index(drop=True)
    no_claims_data = claims_df.loc[claims_df.cord_uid.isin(no_claims_cord_uid), :]\
                              .copy().reset_index(drop=True)

    return claims_data, no_claims_data


def tokenize_section_text(input_data: pd.DataFrame):
    """
    Tokenize section text to sentences.

    :param input_data: pandas dataframe with publication text
    :retunr: Dataframe with section text tokenized to sentences
    """
    # Empty dictonary to store the tokenized text
    text_dict = {}
    # Dictionary iterator
    k = 0

    # Loop through the sections and tokenize text to sentences
    for i, text in enumerate(input_data.text):
        for sent in sent_tokenize(text):
            text_dict[k] = {'cord_uid': input_data.cord_uid[i],
                            'section': input_data.section[i],
                            'text': input_data.text[i],
                            'drug_terms_used': input_data.drug_terms_used[i],
                            'claims': sent}
            k = k + 1

    return pd.DataFrame.from_dict(text_dict, "index")


def pair_similar_claims(claims_data: pd.DataFrame, nlp):
    """
    Pair similar claims.

    :param claims_data: pandas dataframe with cord 19 claims
    :param nlp: Scispacy nlp object
    :return: Dataframe of paired claims
    """
    # Extract list of drug terms present across all claims
    # Note: 'drug_terms_used' consists of drug terms present in the section in which the claim appears
    drug_terms = []
    for drugs in claims_data.drug_terms_used:
        drug_terms = drug_terms + str(drugs).split(',')
    drug_terms = list(set(drug_terms))
    drug_terms.append('acei/arb')

    # Filter to claims that contain drug terms
    sentences_to_keep = [any(True for d in drug_terms if d in c) for c in claims_data.claims]
    claims_data = claims_data[sentences_to_keep].reset_index(drop=True)
    # Add a new column for storing the drug terms present in each claim
    claims_data['drug_terms_mention'] = [[d for d in drug_terms if d in c] for c in claims_data.claims]

    drug_terms_mentions_flat = [d for d_list in claims_data['drug_terms_mention'] for d in d_list]
    drug_terms_mentions_flat = list(set(drug_terms_mentions_flat))

    paper_pairs_filt = []
    # Loop through drugs and filter to claims that mention the drug term
    for d in drug_terms_mentions_flat:
        claims_with_drug_index = [d in d_list for d_list in claims_data.drug_terms_mention]
        claims_with_drug = claims_data[claims_with_drug_index]
        # Pair all claims with the same drug mention
        paper_pairs = list(combinations(claims_with_drug.index, 2))
        # Filter to claim pairs that come from different papers
        for i, j in paper_pairs:
            if claims_with_drug.cord_uid[i] != claims_with_drug.cord_uid[j]:
                # if any(d1 in claims_data.drug_terms_mention[i] for d1 in claims_data.drug_terms_mention[j]):
                paper_pairs_filt.append((i, j))
    paper_pairs_filt = list(set(paper_pairs_filt))

    # Calculate scispacy vector for each claim
    claims_data['w2vVector'] = [nlp(c).vector.reshape(1, -1) for c in claims_data.claims]

    # Empty dictonary to store the similar claim pairs
    claim_pairs_dict = {}
    # Dictionary iterator
    k = 0

    # Initialize just-in-time compiler for efficient parallel processing
    jit(nopython=True, parallel=True)

    # For each pair of claims, calculate cosine similarity between the respective scispacy vectors
    # and keep only those pairs with at least 50% similarity
    for i, j in paper_pairs_filt:
        cos_sim = cosine_similarity(claims_data.w2vVector[i], claims_data.w2vVector[j])[0][0]
        if cos_sim >= 0.5:
            claim_pairs_dict[k] = {'paper1_cord_uid': claims_data.cord_uid[i],
                                   'paper2_cord_uid': claims_data.cord_uid[j],
                                   'text1': claims_data.claims[i],
                                   'text2': claims_data.claims[j],
                                   'similarity_score': cos_sim,
                                   'drugs1': claims_data.drug_terms_mention[i],
                                   'drugs2': claims_data.drug_terms_mention[j]}
            k = k + 1

    return pd.DataFrame.from_dict(claim_pairs_dict, "index")


def add_cord_metadata(input_data, metadata_path):
    """
    Add paper publish time and title metadata to the given cord claim pairs.

    :param input_data: pandas dataframe with cord claim pairs
    :param metadata: path to cord metadata.csv
    :return: Merged dataframe
    """
    # Read metadata
    metadata = pd.read_csv(metadata_path)
    metadata = metadata[['cord_uid', 'publish_time', 'title']]

    # Add title and publish time for first claim's paper
    input_data = pd.merge(input_data, metadata, how='inner',
                          left_on='paper1_cord_uid',
                          right_on='cord_uid')
    cols_rename = {'title': 'title1', 'publish_time': 'publish_time1'}
    input_data.drop(columns='cord_uid', inplace=True)
    input_data.rename(columns=cols_rename, inplace=True)

    # Add title and publish time for second claim's paper
    input_data = pd.merge(input_data, metadata, how='inner',
                          left_on='paper2_cord_uid',
                          right_on='cord_uid')
    cols_rename = {'title': 'title2', 'publish_time': 'publish_time2'}
    input_data.drop(columns='cord_uid', inplace=True)
    input_data.rename(columns=cols_rename, inplace=True)

    return input_data
