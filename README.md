# Ethos Tribo

Sistema independente de encontros: FastAPI, SQLAlchemy, PostgreSQL e HTML/CSS originais.

O domínio e os serviços do site existente não fazem parte deste deploy.
O evento inicial é criado sem participantes e sem chave Pix. Dados reais são cadastrados no painel.
Credenciais e dados do banco ficam exclusivamente nas variáveis privadas do Render.

## Execução

Instale requirements.txt, configure DATABASE_URL com TLS, SECRET_KEY, BASE_URL,
APP_ENV=production, SECURE_COOKIES=true, ADMIN_EMAIL e ADMIN_PASSWORD_HASH.
Execute `python -m app.bootstrap` e `uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 1`.

## Testes

`pip install -r requirements-dev.txt` e `pytest -q`.
Os participantes e credenciais dos testes são exemplos fictícios.

Pix é conferido manualmente. Contas deste sistema são independentes das contas do site original.
Não há verificação de e-mail nem recuperação de senha por e-mail.
