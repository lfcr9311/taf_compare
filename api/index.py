"""
Entrypoint serverless da Vercel.

Exporta o objeto WSGI do Flask. Nao chama app.run(): quem serve aqui e o
runtime da Vercel, nao o processo. O dataframe e montado uma vez por cold
start, a partir de data/malha.parquet (~2 MB, load em ~0,2 s).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as webapp  # noqa: E402

if webapp.DF is None:
    webapp.DF = webapp.load()

app = webapp.app
