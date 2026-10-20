# 🤖 Robô de Bitcoin (BTC/USDT) — modo SIMULADO

Robô que analisa o Bitcoin em tempo real e faz **operações simuladas** (paper trading) com **US$ 1.000 fictícios**. Ele tem um painel para usar no iPhone, manda alertas pelo Telegram e permite testar a estratégia no passado (backtest).

> 🛡️ **Nenhum dinheiro real é usado.** O robô não tem código para enviar ordens a corretoras, não aceita chaves de API de corretora e se recusa a iniciar se alguém tentar desligar o modo simulado.
>
> ⚠️ Projeto educacional. Não é recomendação de investimento. Resultados passados (inclusive de backtest) não garantem resultados futuros.

---

## Sumário

1. [O que o robô faz](#1-o-que-o-robô-faz)
2. [As regras de COMPRA, VENDA e AGUARDAR](#2-as-regras-de-compra-venda-e-aguardar)
3. [Gestão de risco](#3-gestão-de-risco)
4. [Passo a passo pelo iPhone](#4-passo-a-passo-pelo-iphone) ← **comece por aqui**
5. [Custos](#5-custos)
6. [Problemas comuns](#6-problemas-comuns)
7. [Configurações (variáveis de ambiente)](#7-configurações-variáveis-de-ambiente)
8. [Para quem programa](#8-para-quem-programa)

---

## 1. O que o robô faz

| Função | Como funciona |
|---|---|
| Dados reais | Candles e volume de APIs **públicas** (sem login): Binance → OKX → Coinbase. Se uma falhar, usa a próxima. |
| Atualização | A cada 20 segundos; analisa cada candle de **5 min** que fecha, com confirmação do gráfico de **15 min**. |
| Indicadores | EMA (médias móveis), RSI, MACD, volume relativo e ATR (volatilidade, usado no stop). |
| Decisão | **COMPRA**, **VENDA** ou **AGUARDAR**, com o motivo de cada condição. Sem dados suficientes = AGUARDAR. |
| Simulação | Saldo fictício de US$ 1.000, com taxa (0,1%) e slippage (0,05%) em cada execução. |
| Proteções | Stop-loss, alvo (take-profit), risco máximo de 1% por operação e parada no dia ao perder 3%. |
| Alertas | Telegram: sinais, compras/vendas simuladas com resultado, parada diária, falhas de dados e resumo do dia. |
| Painel | Gráfico, saldo, histórico, lucro/prejuízo, taxa de acerto, drawdown, botão de pausar, backtest e registro de erros. |
| Backtest | Testa no histórico real e depois em dados "novos" (que não foram usados para escolher os parâmetros). |
| Recuperação | Se o servidor reiniciar, continua de onde parou, sem repetir operações. |

## 2. As regras de COMPRA, VENDA e AGUARDAR

O robô opera só **comprado** (como quem compra Bitcoin à vista). **VENDA** significa encerrar a posição ou, se não houver posição, ficar de fora.

**COMPRA** — precisa de **todas** as 6 condições:

1. Tendência de alta no 15 min: EMA20 acima da EMA50
2. Preço do 15 min acima da EMA50
3. No 5 min: EMA9 acima da EMA21
4. No 5 min: MACD (histograma) positivo e subindo
5. No 5 min: RSI entre 50 e 70 (força, sem estar "esticado")
6. Volume do candle maior ou igual à média dos 20 candles anteriores

**VENDA** — basta **uma**:

- Momento de baixa no 5 min: EMA9 abaixo da EMA21 **e** MACD negativo
- Tendência de baixa no 15 min: EMA20 abaixo da EMA50 **e** preço abaixo da EMA50

**AGUARDAR** — em todos os outros casos. Também é a resposta automática quando:

- há menos de 100 candles de 5 min ou de 15 min;
- faltam candles na sequência (falha da fonte de dados);
- os dados estão atrasados mais de 10 minutos;
- algum indicador ainda não pode ser calculado.

Nesses casos o painel mostra "Dados insuficientes — nenhum sinal gerado" e o motivo exato.

**Quando a ordem simulada é executada?** O sinal é calculado quando o candle de 5 min **fecha**. A ordem simulada é executada na **abertura do candle seguinte**, que é o primeiro preço disponível depois da decisão. O robô e o backtest seguem exatamente essa mesma regra, então o backtest não "vê o futuro".

## 3. Gestão de risco

- **Stop-loss:** entrada − 1,5 × ATR(14). O ATR mede o tamanho médio dos candles recentes.
- **Alvo (take-profit):** 2 × a distância até o stop.
- **Risco de 1%:** o tamanho da posição é calculado para que, se o stop for atingido, a perda (já com taxas e slippage) seja no máximo 1% do saldo. A posição também nunca passa do dinheiro disponível (sem alavancagem).
- **Trava diária de 3%:** se a perda do dia chegar a 3% do saldo inicial (US$ 30), a posição é fechada e o robô não abre novas operações até o dia seguinte (horário de Brasília). Perto do limite, o risco da próxima operação é reduzido para não ultrapassá-lo.
- **Stop e alvo no mesmo candle:** considera-se o pior caso (stop).
- **Preço que "pula" o stop (gap):** a saída é feita no preço real de abertura, que é pior que o stop, como aconteceria de verdade.
- **Pausar:** o botão do painel impede novas compras. Uma posição aberta continua protegida por stop e alvo.

Esses limites são travados no código: não é possível configurar risco acima de 1% nem perda diária acima de 3%.

---

## 4. Passo a passo pelo iPhone

Faça **uma etapa de cada vez**. Nada aqui foi configurado ainda: você vai criar cada coisa. Os nomes dos botões podem mudar um pouco quando os sites são atualizados.

### Etapa 0 — O que você vai precisar

- iPhone com **Safari** e o app **Telegram** instalado
- Sua conta do **GitHub** (você já tem: é onde este código está)
- Um **e-mail**
- Um **cartão de crédito internacional**, para a hospedagem paga recomendada (cerca de US$ 7,25/mês; veja [Custos](#5-custos)). Também existe uma opção grátis, mais limitada.

### Etapa 1 — Ver os testes rodando no GitHub (grátis, sem configurar nada)

A cada atualização do código, o GitHub roda sozinho todos os testes e um backtest com dados reais.

1. No Safari, abra: `https://github.com/Davicaldeira1/-robo-trader-ia`
2. Se a página aparecer na versão para celular, role até o fim e toque em **"Desktop"** para ver as abas completas (opcional).
3. Toque na aba **Actions**.
4. Toque na execução mais recente chamada **"Testes e backtest"**.
   - ✅ verde = tudo certo · ❌ vermelho = algo falhou
5. Role a página para baixo até o **Summary** (resumo): o **resultado do backtest** aparece em tabelas.

Para rodar o backtest de novo com outro período: **Actions** → **Testes e backtest** (lista à esquerda ou menu) → botão **Run workflow** → digite os dias (ex.: 60) → **Run workflow**.

### Etapa 2 — Criar o seu bot no Telegram

1. Abra o **Telegram** e toque na **lupa** (busca).
2. Procure por **`@BotFather`**. Escolha o que tem o **selo azul de verificado**.
3. Toque em **Iniciar** (ou envie `/start`).
4. Envie: **`/newbot`**
5. Ele pede um **nome**. Envie, por exemplo: `Meu Robo BTC`
6. Ele pede um **nome de usuário**, que precisa terminar em `bot`. Envie, por exemplo: `meu_robo_btc_123_bot`. Se já existir, tente outro.
7. Ele responde com o **token**, parecido com `1234567890:AAH...xyz`.
   - **Toque e segure** o token para copiar e guarde no app **Notas**.
   - 🔒 **O token é como uma senha.** Não mostre a ninguém nem coloque no código.

### Etapa 3 — Descobrir o seu chat_id (o "endereço" para onde o bot manda mensagens)

1. Na resposta do BotFather, toque no link do seu bot (`t.me/meu_robo_btc_123_bot`).
2. Toque em **Iniciar** e envie qualquer mensagem, por exemplo: `oi`.
3. No **Safari**, digite este endereço trocando `SEU_TOKEN` pelo token completo:
   ```
   https://api.telegram.org/botSEU_TOKEN/getUpdates
   ```
   Atenção: a palavra `bot` fica **colada** no token. Exemplo: `https://api.telegram.org/bot1234567890:AAH...xyz/getUpdates`
4. Vai aparecer um texto técnico. Procure por **`"chat":{"id":`** — o número logo depois é o seu **chat_id** (ex.: `987654321`). Copie e guarde no **Notas**.
   - Se aparecer só `{"ok":true,"result":[]}`, envie outra mensagem ao seu bot e recarregue a página.

> Alternativa: depois de publicar o robô (Etapa 4), o painel tem o botão **"Descobrir meu chat_id"**.

### Etapa 4 — Publicar o robô na nuvem (Render)

**4.1 — Criar a conta**

1. No Safari, abra `https://render.com` e toque em **Get Started** (ou **Sign Up**).
2. Escolha **GitHub** para entrar com a sua conta do GitHub e autorize.
3. Para o plano recomendado (pago), cadastre o cartão: menu da conta → **Billing** → adicionar cartão.

**4.2 — Criar o serviço a partir deste projeto**

1. No painel do Render, toque em **New +** (ou **New**) → **Blueprint**.
2. Toque para conectar o GitHub, se pedir. Na tela do GitHub, escolha dar acesso ao repositório **`-robo-trader-ia`** e confirme.
3. Selecione o repositório **`-robo-trader-ia`**. Se pedir um ramo (branch), escolha `claude/bitcoin-trading-bot-u5qdw1`.
4. Dê um nome ao Blueprint (ex.: `robo-btc`). O Render lê o arquivo `render.yaml` e mostra o serviço **robo-trader-btc** (região Frankfurt, plano Starter, disco de 1 GB).
5. Ele vai pedir 3 valores. Preencha:
   - **`DASHBOARD_PASSWORD`**: invente uma senha forte para o painel (guarde no Notas)
   - **`TELEGRAM_BOT_TOKEN`**: o token da Etapa 2
   - **`TELEGRAM_CHAT_ID`**: o número da Etapa 3
6. Toque em **Apply** (ou **Deploy Blueprint** / **Create**).
7. Espere de 3 a 6 minutos. Quando o status ficar **Live** (verde), está no ar.
8. Você vai receber no Telegram: **"🤖 Robô BTC/USDT iniciado em modo SIMULADO"**.

**Onde fica o endereço do painel:** no Render, toque no serviço **robo-trader-btc**. No topo aparece um link parecido com `https://robo-trader-btc.onrender.com` (pode ter letras a mais no final).

**Para mudar uma variável depois** (ex.: trocar a senha): serviço **robo-trader-btc** → **Environment** → editar → **Save Changes**. O Render reinicia sozinho e o histórico é mantido (fica no disco).

<details>
<summary><b>Opção grátis (para testar, com limitações)</b></summary>

O plano grátis do Render **não tem disco persistente** e **desliga o servidor depois de 15 minutos sem visitas**. Ou seja: o robô para de analisar e o histórico pode ser apagado quando o servidor reinicia. Serve só para conhecer o painel.

1. **New +** → **Web Service** → escolha o repositório `-robo-trader-ia`.
2. Preencha:
   - **Region:** Frankfurt
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `uvicorn app.web:create_app --factory --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips "*"`
   - **Instance Type:** Free
3. Em **Environment Variables**, adicione `DASHBOARD_PASSWORD`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `PAPER_TRADING=true` e `PYTHON_VERSION=3.12.7`.
4. **Create Web Service**.
</details>

### Etapa 5 — Abrir o painel no iPhone como se fosse um app

1. No Safari, abra o endereço do seu painel (Etapa 4).
2. Digite a senha (`DASHBOARD_PASSWORD`) e toque em **Entrar**. O login dura 30 dias.
3. Toque no botão **Compartilhar** (quadrado com seta para cima, na barra inferior).
4. Role e toque em **Adicionar à Tela de Início** → **Adicionar**.
5. Pronto: o ícone **Robô BTC** fica na tela do iPhone, como um app.

**O que você vê no painel:**

- **Sinal agora**: COMPRA / VENDA / AGUARDAR, com o preço. Toque em "Ver as condições analisadas" para ver ✅/❌ em cada regra.
- **Saldo fictício**, **Resultado hoje** (com a barra do limite diário), **Taxa de acerto**, **Drawdown máximo** (maior queda do saldo a partir de um pico) e **Lucro/prejuízo realizado**.
- **Gráfico** de 5 ou 15 minutos, com as médias móveis e as marcações ▲ C (compra) e ▼ V (venda). Toque no gráfico para ver os valores.
- **Curva do saldo**, **Histórico de operações**, **Backtest**, **Telegram** e **Registro de eventos e erros**.
- Botão fixo embaixo: **⏸️ Pausar robô** / **▶️ Retomar robô**.

### Etapa 6 — Testar o Telegram

No painel, desça até **Telegram** e toque em **Enviar mensagem de teste**. Deve chegar: "✅ Teste do robô BTC/USDT: o Telegram está funcionando!".

Você vai receber automaticamente:

- 📈/📉 quando o sinal muda para COMPRA ou VENDA (preço e motivos)
- 🟢 compra simulada (preço, quantidade, stop, alvo, risco)
- ✅/🔴 venda simulada (motivo e **resultado em US$ e %**)
- ⛔ quando a trava diária de 3% é atingida
- ⚠️ quando as fontes de dados falham por 3 ciclos seguidos, e ✅ quando normalizam
- 📅 resumo do dia

### Etapa 7 — Monitoramento (avisar se o robô cair)

O robô já avisa no Telegram quando não consegue obter dados. Para ser avisado também se o **servidor inteiro** cair, use o UptimeRobot (plano grátis para uso pessoal):

1. No Safari, abra `https://uptimerobot.com` e crie uma conta (**Register for FREE**).
2. Toque em **+ New monitor** (ou **Add New Monitor**).
3. Tipo: **HTTP(s)**.
4. URL: o endereço do painel + `/health?strict=true`. Exemplo:
   `https://robo-trader-btc.onrender.com/health?strict=true`
5. Intervalo: **5 minutos**. Marque seu e-mail para os alertas → **Create Monitor**.

O endereço `/health?strict=true` responde **erro (503)** se o robô parou ou se os dados estão sem atualizar há mais de 5 minutos. O endereço `/health` (sem `strict`) mostra o estado em texto e não exige senha.

No Render, o serviço também tem **Logs** (registro técnico detalhado) e **Metrics** (uso de memória e CPU).

### Etapa 8 — Backtest pelo celular

- **Pelo painel:** seção **Backtest** → escolha 7, 14, 30 ou 60 dias → **Rodar backtest**. Leva de 1 a 3 minutos e usa dados reais.
- **Pelo GitHub:** veja a Etapa 1 (botão **Run workflow**).

Como ler o resultado:

- **Parte 2 — dados novos (fora da amostra)** é o mais importante: são dados que **não** foram usados para escolher os parâmetros.
- Compare o **Retorno** com **Comprar e segurar BTC**.
- Com menos de ~30 operações, o resultado é **pouco confiável** (pode ser sorte ou azar). O próprio relatório avisa.
- Taxas e slippage já estão descontados.

---

## 5. Custos

| Item | Custo | Observação |
|---|---|---|
| Render — serviço Starter | **US$ 7/mês** | Fica ligado 24 h. Necessário para o disco. |
| Render — disco de 1 GB | **US$ 0,25/mês** | Guarda saldo e histórico. |
| **Total recomendado** | **≈ US$ 7,25/mês** | Em reais: depende do câmbio, mais o IOF do cartão internacional. |
| Render — plano Free | US$ 0 | Desliga sem visitas e não guarda histórico (só para testar). |
| GitHub (código e testes automáticos) | Grátis | Os testes usam a cota gratuita do GitHub Actions. |
| Telegram | Grátis | |
| Dados de mercado (Binance/OKX/Coinbase) | Grátis | APIs públicas, sem conta. |
| UptimeRobot | Grátis | Plano gratuito para uso pessoal (50 monitores, a cada 5 min). |

Preços conferidos em outubro de 2026 em [render.com/pricing](https://render.com/pricing) e podem mudar. Para cancelar no Render: serviço → **Settings** → **Delete Web Service** (o disco e o histórico são apagados).

## 6. Problemas comuns

| Problema | O que fazer |
|---|---|
| Painel mostra "Sem dados" | As fontes de dados falharam. O robô tenta de novo sozinho a cada 20 s e **não opera** enquanto isso. Veja o "Registro de eventos e erros". Se persistir, confira se a região do Render é Frankfurt (a Binance bloqueia servidores nos EUA; nesse caso o robô usa OKX ou Coinbase). |
| Sinal sempre "AGUARDAR" | É normal: as 6 condições de compra juntas são raras. Toque em "Ver as condições analisadas" para ver o que falta. |
| Telegram não chega | Confira `TELEGRAM_BOT_TOKEN` e `TELEGRAM_CHAT_ID` (Render → Environment). Você precisa ter tocado em **Iniciar** no seu bot. Use o botão "Descobrir meu chat_id" no painel. |
| Esqueci a senha do painel | Render → serviço → **Environment** → altere `DASHBOARD_PASSWORD` → **Save Changes**. |
| "Muitas tentativas" no login | Depois de 5 senhas erradas, espere 5 minutos. |
| "Parado hoje (−3%)" | A trava diária foi atingida. O robô volta sozinho no dia seguinte (horário de Brasília). |
| Quero parar tudo | Botão **Pausar robô** (para de abrir operações). Para desligar o servidor: Render → serviço → **Suspend Service**. |

## 7. Configurações (variáveis de ambiente)

Os segredos ficam **fora do código**, nas variáveis de ambiente do Render. Veja também `.env.example`.

| Variável | Para que serve | Padrão |
|---|---|---|
| `DASHBOARD_PASSWORD` | Senha do painel | (vazio = painel aberto, com aviso) |
| `TELEGRAM_BOT_TOKEN` | Token do bot | — |
| `TELEGRAM_CHAT_ID` | Seu chat_id | — |
| `SECRET_KEY` | Assina o login (o Render gera sozinho) | — |
| `PAPER_TRADING` | Precisa ser `true`. Outro valor impede o robô de iniciar. | `true` |
| `DATA_DIR` | Pasta do banco de dados | `data` (Render: `/var/data`) |
| `INITIAL_BALANCE` | Saldo fictício inicial | `1000` |
| `RISK_PER_TRADE` | Risco por operação (máx. `0.01`) | `0.01` |
| `DAILY_LOSS_LIMIT` | Perda máxima no dia (máx. `0.03`) | `0.03` |
| `FEE_RATE` / `SLIPPAGE_RATE` | Custos simulados por execução | `0.001` / `0.0005` |
| `ATR_STOP_MULT` / `TAKE_PROFIT_R` | Distância do stop / tamanho do alvo | `1.5` / `2.0` |
| `DATA_PROVIDERS` | Ordem das fontes de dados | `binance,okx,coinbase` |
| `TIMEZONE` | Fuso para "virar o dia" | `America/Sao_Paulo` |

O saldo inicial vale só na criação da simulação. Depois disso, o estado salvo no disco é mantido.

---

## 8. Para quem programa

```
app/
  config.py       configurações + travas de segurança (paper trading obrigatório)
  market_data.py  APIs públicas (Binance, OKX, Coinbase) com troca automática
  indicators.py   EMA, RSI, MACD, ATR, volume relativo (causais)
  strategy.py     regras COMPRA / VENDA / AGUARDAR
  analysis.py     monta o "retrato" de cada candle sem olhar o futuro (5m + 15m fechado)
  trader.py       corretora simulada: execução, stop/alvo, risco, trava diária
  engine.py       ciclo ao vivo, persistência, recuperação, alertas
  backtest.py     backtest walk-forward (CLI: python -m app.backtest --days 30)
  storage.py      SQLite (transação única por candle; chaves únicas contra duplicatas)
  notifier.py     Telegram com fila de envio e novas tentativas
  web.py          FastAPI: painel, API, login, /health
  static/         painel (HTML/CSS/JS) e Lightweight Charts™ (TradingView)
tests/            testes automatizados (pytest)
```

Rodar num computador:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest -v                       # testes
python -m app.backtest --days 30          # backtest com dados reais
DASHBOARD_PASSWORD=teste uvicorn app.web:create_app --factory --port 8000
```

Garantias cobertas pelos testes:

- **Sem vazamento do futuro:** o sinal de cada candle é idêntico com o histórico inteiro ou cortado naquele candle. O candle de 15 min só é usado depois de fechado. Mudar preços futuros não altera operações passadas. A escolha de parâmetros não muda se os dados da parte 2 mudarem.
- **Mesmo comportamento ao vivo e no backtest:** o robô rodando candle a candle gera exatamente as mesmas operações que o backtest.
- **Sem duplicatas e com recuperação:** repetir ciclos não duplica nada. Reiniciar no meio dá o mesmo resultado que rodar sem parar. Só um processo pode rodar o robô por banco de dados (trava de arquivo).
- **Falhas:** troca de fonte de dados, bloqueio por região (HTTP 451) sem repetição inútil, todas as fontes fora sem operar, retomada após longa queda (só verifica stop/alvo nos candles perdidos), Telegram fora do ar com reenvio sem duplicar.
- **Indicadores:** o RSI confere com os 19 valores do exemplo clássico de Wilder.
- **Segurança:** recusa `PAPER_TRADING=false` e chaves de corretora; não existe código de ordens reais.
