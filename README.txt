PROJECT NOTES:
The questions were extracted from j-archive.com, from shows that took place between 2013-01-01 and 2013-01-07.

Browse some of the Jeopardy! questions (and answers) in the attached file (input: first two line of each block; expected output: third line of each block (it is the title of a Wikipedia page)). If you don't look at the expected output, can you find the correct Wikipedia page via keyword search?

Expected results (not a hard cutoff): Precision@1 > 0.40 and MRR > 0.50.

It is harder than it seems
Your solution must be generic
Don’t implement anything that is specific to the 100 questions
It must work when the answer is any of the 280k articles
Indexing 280k wikipedia articles is tricky
~123,221,423 tokens
Preprocessing
Parsing the article
Tokenizing, stemming, and so on
Do you index the whole thing?
Lucene vs. your search engine

Reranking the top results with an LLM via prompting is likely to raise your results substantially (and it is not too hard to implement).


Wiki subset not present
