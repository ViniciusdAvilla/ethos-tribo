# Ethos Tribo — gestão de atividades

As atualizações em `updates/` são aplicadas por `ci_neon.py` sobre o pacote original antes dos testes e da inicialização. As imagens originais permanecem no pacote; fotos editadas são persistidas no PostgreSQL.

O painel `/admin/fotos` permite substituir, recortar, girar e restaurar imagens. `/admin/responsaveis` atribui contas existentes a uma ou mais atividades. Administradores designados não recebem acesso global. Cada evento escolhe lista, gratuito ou pago e pode configurar links e check-in.

Pix automático usa Mercado Pago Orders e exige `MP_ACCESS_TOKEN` e `MP_WEBHOOK_SECRET` no ambiente privado do Render. Cadastre a chave de recebimento na conta do provedor e configure notificações Orders em `/webhooks/pix`. Sem as credenciais, eventos pagos não geram cobranças. `PIX_KEY` e `PIX_RECEIVER_NAME` são usados somente para QR estático de eventos por lista, conferidos manualmente. Não adicione dados pessoais, chaves reais ou credenciais ao código.

A autorização de imagem no check-in é opcional, específica para divulgação institucional sem fins lucrativos e pode ser revogada no acompanhamento. O banco guarda texto, aceite e revogação. A organização deve respeitar a situação atual antes de publicar fotos.

Pagamentos integrados são confirmados com consulta autenticada ao provedor e conferência de referência, valor, moeda e status. Há testes simulados do provedor; a ativação real exige teste em sandbox e recebimento real na conta conectada. A exportação PNG é privada, personalizada com título e layout Ethos e paginada em 25 pessoas.

Migrações são aditivas e idempotentes. As inscrições existentes continuam no modo lista. Testes PostgreSQL são executados em um schema descartável da branch dedicada de testes do Neon.
