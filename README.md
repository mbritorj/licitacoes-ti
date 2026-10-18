# Robô de licitações de TI (ISAKMP)

Consulta todo dia o PNCP (Portal Nacional de Contratações Públicas), separa as contratações de TI com propostas em aberto no RJ, SP, MG e ES e publica um painel web com filtros.

- Fonte: API pública de consulta do PNCP. Pela Lei 14.133/2021, órgãos federais, estaduais e municipais publicam ali, mesmo quando a disputa acontece em outro portal (Compras.gov.br, BLL, Licitações-e etc.).
- Modalidades: pregão eletrônico e presencial, concorrência eletrônica e dispensa.
- Classificação por palavras-chave no objeto: **Serviço**, **Equipamento** e **Licença/Software**.
- Para cada licitação encontrada, o robô lê os itens no PNCP e marca se há participação exclusiva ou cota para ME/EPP.
- Licitações só de equipamento ou licença aparecem com o aviso "exige CNAE de comércio", porque a ISAKMP ainda não tem esses códigos. O filtro "Só aderentes ao CNAE atual" esconde essas.
- Roda no GitHub Actions às 06:30 (todo dia) e às 12:30 (dias úteis). Uma execução leva de 15 a 40 minutos, porque a API do PNCP limita a cerca de 12 chamadas por minuto.

## Instalação (uma vez, uns 15 minutos)

1. **Criar o repositório.** Em github.com, clique em **New repository**, dê um nome (ex.: `licitacoes-ti`) e marque **Public**. No plano gratuito, o painel (GitHub Pages) só funciona em repositório público. O conteúdo são dados públicos de licitação.
2. **Enviar os arquivos.** Na página do repositório, clique em **uploading an existing file** e arraste todo o conteúdo desta pasta (incluindo a pasta `.github`). No Mac, a pasta `.github` fica oculta no Finder: use `Cmd + Shift + .` para mostrá-la. Clique em **Commit changes**.
3. **Dar permissão de escrita ao robô.** Settings > Actions > General > Workflow permissions > marque **Read and write permissions** > Save.
4. **Ativar o painel.** Settings > Pages > Source: **Deploy from a branch** > Branch: `main`, pasta `/docs` > Save. Em um ou dois minutos o endereço aparece no topo da página, no formato `https://SEU-USUARIO.github.io/licitacoes-ti/`.
5. **Primeira execução.** Aba **Actions** > **Robô de licitações** > **Run workflow**. Quando terminar (ícone verde), abra o endereço do painel.

Salve o endereço do painel nos favoritos do celular e do computador.

## Ajustes no dia a dia

Tudo fica em `config.json`, que dá para editar direto no site do GitHub (ícone de lápis):

- `ufs`: estados pesquisados.
- `modalidades`: códigos do PNCP (4 concorrência eletrônica, 5 concorrência presencial, 6 pregão eletrônico, 7 pregão presencial, 8 dispensa, 12 credenciamento).
- `categorias`: termos de busca de cada tipo, com peso. Peso 3 basta para incluir a licitação; pesos 1 e 2 precisam somar com outro termo.
- `exclusoes`: termos que derrubam falsos positivos (ex.: "rede de esgoto", "capacitação de servidores").
- `fabricantes_destaque`: aparecem com etiqueta própria no painel (Cisco, Nokia, Huawei etc.).
- `valor_limite_me_epp`: valor até o qual a licitação é marcada como faixa de ME/EPP (R$ 80 mil, art. 48 da LC 123).

Depois de mexer nos termos, rode o workflow manualmente para ver o efeito. O teste em `tests/teste_classificacao.py` roda antes de cada coleta e para a execução se uma alteração fizer o robô deixar de reconhecer casos básicos (ou passar a aceitar "aquisição de medicamentos", por exemplo). Se você incluir um termo novo, vale acrescentar um exemplo lá também.

## Limitações

- A classificação olha o texto do objeto. Objeto genérico ("aquisição de material permanente") sem palavra de TI pode ficar de fora; objeto com termo de TI usado em outro sentido pode entrar. Ajuste os termos conforme aparecerem casos.
- Só entram licitações publicadas no PNCP. Estatais regidas pela Lei 13.303 (Petrobras, por exemplo) muitas vezes publicam só no portal próprio.
- O PNCP às vezes fica fora do ar ou devolve erro. Nesse caso o painel mostra "coleta parcial" ou "última coleta falhou" e mantém os dados anteriores daquela UF e modalidade.
- O GitHub pode desativar workflows agendados de repositórios sem atividade por 60 dias. Se o painel parar de atualizar, entre na aba Actions e reative.

## Rodar no computador (opcional)

Com Python 3.9 ou mais novo, sem instalar nada:

```
python3 robo.py --max-paginas 2     # teste rápido
python3 robo.py                     # coleta completa
```

Para abrir o painel localmente: `cd docs && python3 -m http.server` e acesse `http://localhost:8000`.
