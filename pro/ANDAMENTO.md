# SigaLicita Pro — anotações do projeto

Site com login que junta as ferramentas do licitante (hoje artifacts do Claude) num lugar só.
Dona: Soluções Comerciais (Brasília/DF). Fica nesta pasta `pro/` do repositório do SigaLicita.

## Decisões
- Nome **SigaLicita Pro**; domínio **sigalicitapro.com.br** (livre, ainda não registrado).
- Visual igual ao SigaLicita: azul-marinho, dourado, fonte Plus Jakarta Sans, logo do SigaLicita + selo PRO dourado, lua para versão clara/escura (`pro_tema` no navegador).
- Menu: Encontrar (Buscar licitações, Radar, SigaScore) · Preparar (Leitor de edital [IA], Baixar editais, Buscar fornecedor, Gerador de proposta) · Disputar (Calculadora de lances, Manifestação [IA]) · Empresa (Documentos e certidões, Consulta CNPJ, Controle de caixa, Meus sites) · Minha conta · Sair. No celular: barra embaixo (Início, Buscar, Lances, Propostas, Mais).
- Início: "Bom dia, [nome]", 4 números (oportunidades salvas, disputas da semana, propostas no mês, leituras de edital "12 de 30"), cartões das ferramentas.
- Para vender depois: cada cliente com login e dados separados; área do administrador só para a dona; limite de leituras de IA por plano + pacote extra; cobrança pelo Asaas.
- Planos (exemplo): SigaLicita R$ 79,90 · Pro R$ 149,90 · Combo R$ 199,90. Um login só para SigaLicita + Pro (Etapa 2).
- IA: uma chave da API da Anthropic (da dona). Ferramentas com IA ficam "EM BREVE" até ela colocar crédito.
- Sempre mostrar prévia antes de publicar. Trabalhar em partes pequenas.

## Etapas
1. Site com login + menu + ferramentas sem IA, só para a dona usar.
2. Ligar ao SigaLicita (login único, oportunidades salvas no servidor).
3. Planos, combo, pacotes, Asaas, termos de uso/LGPD, lançamento.

## Feito
- [x] Login (e-mail/senha, senha cifrada PBKDF2, cookie assinado 30 dias), sair, tela inicial, menu, lua, celular.
- Conta da administradora criada pelas variáveis PRO_ADMIN_EMAIL / PRO_ADMIN_SENHA / PRO_ADMIN_NOME / PRO_ADMIN_EMPRESA.

## Falta (próximas partes)
- [ ] Banco de dados de verdade (Postgres grátis: Supabase ou Neon). Hoje é SQLite local — no Render grátis o arquivo some a cada deploy.
- [ ] Colocar no ar no Render (Root Directory `pro`, start `python3 app.py`, ignorar a pasta pro no deploy do SigaLicita).
- [ ] Trazer as ferramentas, uma por vez (começar pelas sem IA: Calculadora, Caixa, Radar, Baixar editais...).
- [ ] Minha conta, área do administrador, esqueci a senha, criar conta.
