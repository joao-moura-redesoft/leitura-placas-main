import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2
import logging
logging.basicConfig(level=logging.INFO, format='%(message)s')

from ocr import AutoOCR
ocr = AutoOCR()
ocr.carregar()

casos = [
    ('testes/fotos/synthetic_NOP5Q67.jpg', 'NOP5Q67'),
    ('testes/fotos/synthetic_moto_mercosul_KQR5Q89.jpg', 'KQR5Q89'),
    ('testes/fotos/synthetic_moto_mercosul_EQG0Q00.jpg', 'EQG0Q00'),
]

for fname, expected in casos:
    img = cv2.imread(fname)
    print(f'\n=== {fname} | expected={expected} | shape={img.shape} ===')
    det = ocr.ler_detalhado(img)
    print(f'  FINAL: placa={det["placa"]} conf={det["confianca"]}')
    for d in det['detalhes']:
        print(f'  engine={d["engine"]} placa={d["placa"]} conf={d["confianca"]}')
