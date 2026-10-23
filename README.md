# Robô de licitações de TI (ISAKMP)

Consulta todo dia o PNCP (Portal Nacional de Contratações Públicas), separa as contratações de TI com propostas em aberto e publica um painel web com filtros.

- Fonte: API pública de consulta do PNCP. Pela Lei 14.133/2021, órgãos federais, estaduais e municipais publicam ali, mesmo quando a disputa acontece em outro portal (Compras.gov.br, BLL, Licitações-e etc.).
- Modalidades: pregão eletrônico e presencial, concorrência eletrônica e dispensa.
- Classificação por palavras-chave no objeto: **Serviço**, **Equipamento** e **Licença/Software**.
- Abrangência: licenças e software no Brasil todo (entrega eletrônica, sem logística); serviços, equipamentos e licitações que só citam fabricante em RJ, SP, MG e ES. Ajuste em `config.json` (`ufs_por_categoria` e `ufs_foco`).
- Para cada licitação encontrada, o robô lê os itens no PNCP e marca se há participação exclusiva ou cota para ME/EPP e se o orçamento é sigiloso.
- Licitações só de equipamento ou licença aparecem com o aviso "exige CNAE de comércio", porque a ISAKMP ainda não tem esses códigos. O filtro "Só aderentes ao CNAE atual" esconde essas.
- Roda no GitHub Actions às 06:30 (todo dia) e às 12:30 (dias úteis).

## Como funciona a coleta (desde 23/10/2026)

A coleta cobre as 27 UFs e é compartilhada com o painel da Blue Belt (repositório `licitacoes-bluebelt`).

1. **Coleta por região, em paralelo.** O workflow roda 6 coletores ao mesmo tempo, definidos em `coleta.json`: SP, MG, RJ-ES, SUL, NE e NO-CO. Cada um respeita a pausa de 5 segundos entre chamadas ao PNCP. Não aumente muito o número de grupos: o PNCP pode bloquear tráfego excessivo, e o bloqueio para os dois painéis.
2. **Coleta incremental.** A primeira coleta de cada grupo lista tudo o que está com proposta em aberto. As seguintes buscam só o que foi publicado ou alterado desde a última (com 1 dia de sobreposição) e somam ao que já se conhecia. A cada 7 dias o grupo refaz a coleta completa, para corrigir qualquer diferença. Para forçar uma coleta completa: Actions > Robô de licitações > Run workflow > marque "Forçar coleta completa".
3. **Painel.** `painel.py` junta os grupos, classifica com o `config.json`, consulta os itens das licitações novas e publica o site. Os dados brutos de cada grupo ficam publicados em `/bruto/` no mesmo endereço do painel; é de lá que o painel da Blue Belt lê.
4. **Falhas.** Se um grupo falhar, o painel usa a última coleta publicada daquele grupo e mostra o aviso no topo.

Os dados não são mais gravados no repositório (o painel é publicado direto pelo GitHub Pages), então o histórico do Git não cresce a cada execução.

Arquivos: `coletor.py` (coleta por grupo), `painel.py` (classificação e site), `pncp.py` (funções comuns, mesma cópia no repositório da Blue Belt), `coleta.json` (grupos de UFs e modalidades), `config.json` (classificação e abrangência), `docs/` (página do painel).

## Instalação (uma vez, uns 15 minutos)

1. **Criar o repositório.** Em github.com, clique em **New repository**, dê um nome (ex.: `licitacoes-ti`) e marque **Public**. No plano gratuito, o painel (GitHub Pages) só funciona em repositório público. O conteúdo são dados públicos de licitação.
2. **Enviar os arquivos.** Na página do repositório, clique em **uploading an existing file** e arraste todo o conteúdo desta pasta (incluindo a pasta `.github`). No Mac, a pasta `.github` fica oculta no Finder: use `Cmd + Shift + .` para mostrá-la. Clique em **Commit changes**.
3. **Dar permissão de escrita ao robô.** Settings > Actions > General > Workflow permissions > marque **Read and write permissions** > Save.
4. **Ativar o painel.** Settings > Pages > Build and deployment > Source: **GitHub Actions**. O endereço fica no formato `https://SEU-USUARIO.github.io/licitacoes-ti/`.
5. **Primeira execução.** Aba **Actions** > **Robô de licitações** > **Run workflow**. Quando terminar (ícone verde), abra o endereço do painel.

Salve o endereço do painel nos favoritos do celular e do computador.

## Ajustes no dia a dia

Tudo fica em `config.json`, que dá para editar direto no site do GitHub (ícone de lápis):

- `ufs`: UFs consideradas no painel (`"todas"` ou uma lista).
- `ufs_por_categoria`: em quais UFs cada categoria vale. Categoria fora da lista vale em todas.
- `ufs_foco`: UFs das licitações que entram só pelo nome do fabricante.
- Grupos de UFs e modalidades da coleta ficam em `coleta.json` (modalidades: 4 concorrência eletrônica, 5 concorrência presencial, 6 pregão eletrônico, 7 pregão presencial, 8 dispensa, 12 credenciamento).
- `categorias`: termos de busca de cada tipo, com peso. Peso 3 basta para incluir a licitação; pesos 1 e 2 precisam somar com outro termo.
- `exclusoes`: termos que derrubam falsos positivos (ex.: "rede de esgoto", "capacitação de servidores").
- `fabricantes_destaque`: aparecem com etiqueta própria no painel (Cisco, Nokia, Huawei etc.).
- `valor_limite_me_epp`: valor até o qual a licitação é marcada como faixa de ME/EPP (R$ 80 mil, art. 48 da LC 123).

Depois de mexer nos termos, rode o workflow manualmente para ver o efeito. Os testes em `tests/` (classificação e um fluxo completo com o PNCP simulado) rodam antes de cada coleta e para a execução se uma alteração fizer o robô deixar de reconhecer casos básicos (ou passar a aceitar "aquisição de medicamentos", por exemplo). Se você incluir um termo novo, vale acrescentar um exemplo lá também.

## Limitações

- A classificação olha o texto do objeto. Objeto genérico ("aquisição de material permanente") sem palavra de TI pode ficar de fora; objeto com termo de TI usado em outro sentido pode entrar. Ajuste os termos conforme aparecerem casos.
- Só entram licitações publicadas no PNCP. Estatais regidas pela Lei 13.303 (Petrobras, por exemplo) muitas vezes publicam só no portal próprio.
- O PNCP às vezes fica fora do ar ou devolve erro. Nesse caso o painel mostra "coleta parcial" ou "última coleta falhou" e mantém os dados anteriores daquela UF e modalidade.
- O GitHub pode desativar workflows agendados de repositórios sem atividade por 60 dias. Como os dados não são mais gravados no repositório, isso pode acontecer. Se o painel parar de atualizar, entre na aba Actions e reative.
- A coleta incremental depende do endpoint de atualização do PNCP. Se ele for recusado, o coletor tenta o de publicação (que não pega prorrogações) e, se também falhar, faz a coleta completa.

## Rodar no computador (opcional)

Com Python 3.9 ou mais novo, sem instalar nada:

```
python3 coletor.py --grupo RJ-ES --completa --max-paginas 2 --saida bruto   # teste rápido de um grupo
python3 painel.py --bruto bruto --grupos RJ-ES --site site --offline         # painel sem consultar itens
python3 tests/teste_fluxo.py                                                 # fluxo completo com PNCP simulado
```

Para abrir o painel localmente: `cd site && python3 -m http.server` e acesse `http://localhost:8000`.
