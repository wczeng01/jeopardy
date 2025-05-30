import os
import re
import pickle
import numpy as np
from sentence_transformers import SentenceTransformer, CrossEncoder
import faiss
from rank_bm25 import BM25Okapi
from sklearn.linear_model import LogisticRegression
import time
import torch

# Paths and parameters
WIKI_DIR = "wiki-subset"
QUESTIONS_FILE = "questions.txt"
BM25_INDEX_FILE = "bm25.pkl"
FAISS_INDEX_FILE = "faiss_hnsw.index"
EMB_FILE = "embeddings.npy"
RANKER_FILE = "ranker.pkl"
TOP_K = 10          # Number of dense (FAISS) hits
BM25_K = 50         # Number of BM25 hits
MODEL_NAME = 'all-mpnet-base-v2'
CROSS_ENCODER_MODEL = 'cross-encoder/ms-marco-MiniLM-L-12-v2'
SNIPPET_WORDS = 500
BATCH_SIZE = 64

# 1. Load Wikipedia docs with categories and section headers
def load_documents():
    titles, texts, categories, section_headers = [], [], [], []
    tpl_re = re.compile(r"\[tpl\].*?\[/tpl\]")
    title, buffer, cats, headers = None, [], [], []
    for root, _, files in os.walk(WIKI_DIR):
        for fname in files:
            with open(os.path.join(root, fname), 'r', encoding='utf-8') as f:
                for line in f:
                    clean = tpl_re.sub('', line).strip()
                    m_title = re.match(r'^\[\[(.+?)\]\]', clean)
                    m_sec = re.match(r'^=(=+)\s*(.+?)\s*\1=', clean)
                    if m_title:
                        if title:
                            titles.append(title)
                            texts.append(' '.join(buffer))
                            categories.append(cats)
                            section_headers.append(headers)
                        title = m_title.group(1)
                        buffer, cats, headers = [], [], []
                    elif clean.startswith('CATEGORIES:'):
                        cats = [c.strip().lower() for c in clean.split(':',1)[1].split(',')]
                    elif m_sec:
                        headers.append(m_sec.group(2).lower())
                    else:
                        if title:
                            buffer.append(clean)
                if title:
                    titles.append(title); texts.append(' '.join(buffer)); categories.append(cats); section_headers.append(headers)
    print(f"[DEBUG] Parsed {len(titles)} docs, with section headers and categories")
    return titles, texts, categories, section_headers

# 2. Load Jeopardy questions
def load_questions():
    queries, block = [], []
    with open(QUESTIONS_FILE, 'r', encoding='utf-8') as f:
        for raw in f:
            line = raw.strip()
            if not line and len(block)==3:
                queries.append((block[0].lower(), block[1], block[2])); block=[]
            elif line:
                block.append(line)
        if len(block)==3:
            queries.append((block[0].lower(), block[1], block[2]))
    print(f"[DEBUG] Loaded {len(queries)} Jeopardy queries")
    return queries

# 3. BM25 index load/build
def get_bm25(texts):
    if os.path.exists(BM25_INDEX_FILE):
        with open(BM25_INDEX_FILE,'rb') as f: bm25, tokenized = pickle.load(f)
    else:
        tokenized = [doc.lower().split() for doc in texts]
        bm25 = BM25Okapi(tokenized)
        with open(BM25_INDEX_FILE,'wb') as f: pickle.dump((bm25, tokenized), f)
    return bm25, tokenized

# 4. FAISS index + embeddings load/build
def get_faiss_index(texts, model):
    if os.path.exists(FAISS_INDEX_FILE) and os.path.exists(EMB_FILE):
        index = faiss.read_index(FAISS_INDEX_FILE)
        embeddings = np.load(EMB_FILE)
    else:
        embeddings=[]
        for i in range(0,len(texts),BATCH_SIZE):
            emb = model.encode(texts[i:i+BATCH_SIZE], batch_size=BATCH_SIZE,
                               show_progress_bar=True, convert_to_numpy=True)
            embeddings.append(emb)
        embeddings = np.vstack(embeddings)
        faiss.normalize_L2(embeddings)
        np.save(EMB_FILE, embeddings)
        dim = embeddings.shape[1]
        index = faiss.IndexHNSWFlat(dim,32)
        index.hnsw.efConstruction=200; index.hnsw.efSearch=50
        index.add(embeddings)
        faiss.write_index(index, FAISS_INDEX_FILE)
    print(f"[DEBUG] Embeddings shape: {embeddings.shape}")
    return index, embeddings

# 5. Train ranker with added features
def train_ranker(titles, texts, categories, headers, bm25, tokenized, index, embeddings, dense_model, cross_encoder, queries):
    features, labels, ce_pairs = [], [], []
    print(f"[DEBUG] Train data gather for {len(queries)} queries...")
    for qcat, clue, expected in queries:
        full_query = f"{qcat}. {clue}"
        clue_tokens = clue.lower().split()
        ans_tokens = expected.lower().split()
        bm_scores = bm25.get_scores(clue_tokens)
        top_bm = np.argsort(bm_scores)[::-1][:BM25_K]
        q_emb = dense_model.encode([full_query], convert_to_numpy=True).flatten()
        faiss.normalize_L2(q_emb.reshape(1,-1))
        sims, idxs = index.search(q_emb.reshape(1,-1), TOP_K)
        union = list(dict.fromkeys(list(idxs[0])+top_bm.tolist()))[:TOP_K+BM25_K]
        for idx in union:
            title_tok = titles[idx].lower().split()
            # features
            bmv = float(bm_scores[idx])
            dnv = float(np.dot(q_emb, embeddings[idx]))
            catv = 1.0 if qcat in categories[idx] else 0.0
            cdv = 1.0/(categories[idx].index(qcat)+1) if qcat in categories[idx] else 0.0
            tov = sum(t in title_tok for t in clue_tokens)
            hov = sum(t in hdr.split() for hdr in headers[idx] for t in clue_tokens)
            aov = sum(t in title_tok for t in ans_tokens)
            features.append([bmv, dnv, None, catv, float(tov), float(hov), float(aov), cdv])
            snippet=' '.join(texts[idx].split()[:SNIPPET_WORDS])
            ce_pairs.append([full_query, snippet])
            labels.append(1 if titles[idx].lower()==expected.lower() else 0)
    print(f"[DEBUG] Examples:{len(features)}, positives:{sum(labels)}")
    print(f"[DEBUG] Scoring {len(ce_pairs)} CE pairs (train)...")
    t0=time.time(); ce_scores = cross_encoder.predict(ce_pairs, batch_size=BATCH_SIZE)
    print(f"[DEBUG] Train CE time: {time.time()-t0:.1f}s")
    for i, sc in enumerate(ce_scores): features[i][2]=float(sc)
    X, y = np.array(features), np.array(labels)
    print(f"[DEBUG] X.shape={X.shape}, y.sum={y.sum()}")
    model = LogisticRegression(max_iter=1000)
    model.fit(X, y)
    print(f"[DEBUG] Coefs={model.coef_}")
    with open(RANKER_FILE,'wb') as f: pickle.dump(model,f)
    print(f"[DEBUG] Ranker size={os.path.getsize(RANKER_FILE)} bytes")
    return model

# 6. Evaluate with new features
def evaluate(titles, texts, categories, headers, bm25, tokenized, index, embeddings, dense_model, cross_encoder, ranker, queries):
    all_feats, all_cands, qmap, cepairs = [],[],[],[]
    print(f"[DEBUG] Eval data gather for {len(queries)} queries...")
    for qi,(qcat, clue, exp) in enumerate(queries):
        full_query = f"{qcat}. {clue}"
        clue_tokens = clue.lower().split(); ans_tokens=exp.lower().split()
        bm_scores = bm25.get_scores(clue_tokens)
        top_bm = np.argsort(bm_scores)[::-1][:BM25_K]
        q_emb = dense_model.encode([full_query], convert_to_numpy=True).flatten()
        faiss.normalize_L2(q_emb.reshape(1,-1))
        sims, idxs = index.search(q_emb.reshape(1,-1), TOP_K)
        union = list(dict.fromkeys(list(idxs[0])+top_bm.tolist()))[:TOP_K+BM25_K]
        for idx in union:
            title_tok = titles[idx].lower().split()
            bmv=float(bm_scores[idx]); dnv=float(np.dot(q_emb,embeddings[idx]));
            catv=1.0 if qcat in categories[idx] else 0.0
            cdv=1.0/(categories[idx].index(qcat)+1) if qcat in categories[idx] else 0.0
            tov=sum(t in title_tok for t in clue_tokens)
            hov=sum(t in hdr.split() for hdr in headers[idx] for t in clue_tokens)
            aov=sum(t in title_tok for t in ans_tokens)
            all_feats.append([bmv,dnv,None,catv,float(tov),float(hov),float(aov),cdv])
            snippet=' '.join(texts[idx].split()[:SNIPPET_WORDS])
            cepairs.append([full_query, snippet]); all_cands.append(titles[idx]); qmap.append(qi)
    print(f"[DEBUG] Scoring {len(cepairs)} CE pairs (eval)...")
    t0=time.time(); ces=cross_encoder.predict(cepairs,batch_size=BATCH_SIZE)
    print(f"[DEBUG] Eval CE time: {time.time()-t0:.1f}s")
    for i, sc in enumerate(ces): all_feats[i][2]=float(sc)
    preds=[[] for _ in queries]
    for feat,cand,qi in zip(all_feats,all_cands,qmap): preds[qi].append((cand, ranker.predict_proba([feat])[0][1]))
    corr, rr=0,0
    for i,(qcat,clue,exp) in enumerate(queries):
        ranked=[c for c,_ in sorted(preds[i],key=lambda x:x[1],reverse=True)][:TOP_K]
        if ranked and ranked[0].lower()==exp.lower(): corr+=1
        r=0
        for j,ttl in enumerate(ranked,1):
            if ttl.lower()==exp.lower(): r=1/j; break
        rr+=r
    tot=len(queries)
    print(f"Precision@1: {corr/tot:.4f}")
    print(f"MRR:         {rr/tot:.4f}")

if __name__=='__main__':
    titles,texts,categories,headers=load_documents()
    queries=load_questions()
    bm25,tokenized=get_bm25(texts)
    dense_model=SentenceTransformer(MODEL_NAME)
    device='cuda' if torch.cuda.is_available() else 'cpu'
    print(f"[DEBUG] CE device={device}")
    cross_encoder=CrossEncoder(CROSS_ENCODER_MODEL,device=device)
    index,embeddings=get_faiss_index(texts,dense_model)
    # load or train ranker with feature-dimension check
    expected_dim = 8
    need_train = True
    if os.path.exists(RANKER_FILE):
        print(f"[DEBUG] Found existing ranker {RANKER_FILE}, loading...")
        with open(RANKER_FILE,'rb') as f:
            ranker = pickle.load(f)
        # check feature dimension matches
        if hasattr(ranker, 'coef_') and ranker.coef_.shape[1] == expected_dim:
            print(f"[DEBUG] Ranker feature dimension matches ({expected_dim}), using cached model")
            need_train = False
        else:
            print(f"[DEBUG] Ranker feature dim {getattr(ranker,'coef_',None).shape if hasattr(ranker,'coef_') else None} != expected {expected_dim}, retraining...")
    if need_train:
        ranker = train_ranker(titles, texts, categories, headers,
                            bm25, tokenized, index, embeddings,
                            dense_model, cross_encoder, queries)
    # now evaluate
    evaluate(titles, texts, categories, headers, bm25, tokenized,
         index, embeddings, dense_model, cross_encoder, ranker, queries)