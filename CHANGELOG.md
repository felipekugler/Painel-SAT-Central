# Changelog — Painel SAT Central

Convenção: [SemVer](https://semver.org/) (MAJOR.MINOR.PATCH). O número atual fica em
`APP_VERSION` (`app.py`) e aparece no rodapé do painel.

O versionamento começou nesta entrada (1.3.0) — mudanças anteriores a ela não foram
catalogadas retroativamente.

## [1.9.5] — 2026-11-13

### Alterado
- Modal "Baixar mais documentos": botão "Salvar seleção (próxima rodada da fila)"
  simplificado para "Salvar seleção". As duas linhas de explicação ("Baixar
  selecionados"/"Salvar seleção") moveram do topo para o rodapé do modal, em negrito e
  sem aspas.
- `INSTALAR.bat`: adicionadas as instruções dos dois passos manuais opcionais que
  faltavam — setup do 2FA automático do SAT (`scripts/setup_totp_sat.py`) e cadastro da
  chave do CapSolver no Cofre (resolução automática de CAPTCHA).

## [1.9.4] — 2026-11-13

### Alterado
- **"Levantamento SAT" redesenhado como documento "sui generis"**: deixou de ser um
  arquivo estático e passou a ser recriado a CADA juntada (1ª movimentação via "Anexar ao
  PAJ" e também cada "Anexar mais" complementar). Agora mostra, em cards separados: um
  card VERDE com exatamente o que está sendo anexado NESTA movimentação (datado de hoje);
  um card CINZA para cada movimentação anterior já registrada (datado com a data real
  daquela movimentação, mais recente primeiro — rastreabilidade); e, por fim, a lista
  (sempre recalculada) do que ainda não foi anexado. "Anexar mais" agora também gera e
  inclui esse PDF automaticamente como 1º anexo.

## [1.9.3] — 2026-11-13

### Corrigido
- **"Levantamento SAT" nunca era anexado de verdade**: o PDF já existia e tinha sido
  validado visualmente, mas nunca foi ligado à rotina real de "Anexar ao PAJ"
  (`processar_paj_stream`) — por isso nenhuma movimentação incluía o documento. Agora ele
  é gerado e salvo na pasta raiz do PAJ, e sempre entra como o 1º anexo da movimentação
  (assistido + instituidor + outros CPFs continuam depois dele). Falha ao gerá-lo não
  impede a juntada dos demais documentos.

## [1.9.2] — 2026-11-13

### Alterado
- Modal "Baixar mais documentos": o botão "Ver log completo" virou um ícone de log (o
  mesmo da aba Individual), no canto superior direito, na mesma linha do título —
  tooltip "log".

## [1.9.1] — 2026-11-13

### Corrigido
- **Documento descartado manualmente voltava a aparecer como "não baixado"**: ao excluir
  um documento já baixado (lixeira "Excluídos"), ele perdia a marca `ja_baixado` no
  inventário — reaparecia no modal "Baixar mais documentos" com checkbox liberado, como
  se nunca tivesse sido baixado (o que também reabriria a possibilidade de a fila tentar
  baixá-lo de novo). Agora o documento continua `ja_baixado=True` para sempre (não
  reabilita novo download); só deixa de contar como "vai ser anexado ao PAJ". O modal
  mostra "Baixado, descartado manualmente" em âmbar (checkbox continua travado) e o PDF
  "Levantamento SAT" passa esse item para a seção "Documentos não anexados".

## [1.9.0] — 2026-11-13

### Corrigido
- **Levantamento travado por antirrobô**: quando a 1ª rodada de levantamento era
  bloqueada por antirrobô para algum tipo de documento, o item ficava marcado
  "levantamento: antirobo" para sempre — já que, a partir da 2ª rodada, só a lista de
  batimento manda e o levantamento nunca mais era refeito. Agora, a cada rodada, se a
  lista de batimento tiver algum item pendente por esse motivo, a fila refaz o
  levantamento (sem tocar no que já foi decidido/baixado) e mescla o resultado — tanto
  para o assistido principal quanto para CPFs extras (instituidor/terceiro/outro
  assistido).

### Alterado
- Modal "Baixar mais documentos": removida a frase com o nome do assistido e a menção ao
  CAPTCHA na introdução; a explicação de "Baixar selecionados" x "Salvar seleção" virou
  duas linhas separadas e mais diretas.

## [1.8.2] — 2026-11-13

### Alterado
- "Levantamento SAT": o card verde de "Documentos anexados ao PAJ" tinha a borda
  encostando no título; aumentada a margem superior. Uma linha em branco a mais antes de
  "Documentos não anexados" começar.

## [1.8.1] — 2026-11-13

### Corrigido
- **Bug real do PyMuPDF**: `fitz.get_text_length()` subestima a largura de certas
  palavras com acentuação (ex.: "Declaração" media ~40,5pt de uma vez, mas a largura
  realmente desenhada é ~45,5pt) — isso colava a palavra seguinte sem espaço nenhum
  ("Declaraçãode Benefícios") sempre que o texto era desenhado palavra por palavra (que
  passou a ser o caso no "Levantamento SAT" com a introdução das cores por trecho,
  abaixo). Corrigido medindo a largura caractere a caractere, que bate com o que é
  desenhado na tela. Mesma correção aplicada por precaução no "Baixar PAJ Completo"
  (quebra de linha da narrativa), que tem o mesmo risco.

### Adicionado
- "Levantamento SAT": o texto entre parênteses de cada documento volta a ter cor (verde
  = baixado, vermelho = falha/antirrobô/indisponível, âmbar = qualquer outro motivo) —
  mesmo critério do modal "Baixar mais" — mas só esse trecho, sem legenda (o rótulo do
  documento continua em preto).
- "Levantamento SAT": a seção "Documentos anexados ao PAJ" agora fica dentro de um card
  verde-claro, para se destacar visualmente de "Documentos não anexados".

## [1.8.0] — 2026-11-13

### Alterado
- "Levantamento SAT" reformulado para ficar mais direto para quem for analisar o PAJ:
  sem legenda e sem cores (texto simples já é autoexplicativo); duas seções —
  "Documentos anexados ao PAJ" (baixados: já anexados antes ou que entram nesta mesma
  movimentação) e "Documentos não anexados" (tudo o mais: nunca baixados, ou baixados e
  descartados manualmente). Dentro de cada seção, continua agrupado por Grupo 1/Grupo
  2/Processos Administrativos — e pelo Instituidor, quando houver.

## [1.7.1] — 2026-11-13

### Adicionado
- Novo status "Baixado, descartado manualmente": quando o Defensor exclui (lixeira
  "Excluídos") um documento já baixado antes de anexar, a lista de documentos ("Baixar
  mais" e "Levantamento SAT") agora mostra essa distinção em vez de deixar a linha sem
  nenhuma indicação — evita a impressão de que o documento nunca foi baixado.

### Alterado
- "Já baixado" → "Baixado" (e "Já anexado ao PAJ" → "Anexado ao PAJ") no texto de status
  por documento — mesmo lugar (modal "Baixar mais" e "Levantamento SAT").

## [1.7.0] — 2026-11-13

### Adicionado
- **"Levantamento SAT"**: novo PDF gerado por `services/levantamento_sat_service.py` —
  anonimizado (só o número do PAJ, sem nome do assistido), com a mesma lista/
  agrupamento/cores do modal "Baixar mais" (verde = já baixado, vermelho = falha/
  antirrobô/indisponível, âmbar = qualquer outro motivo), incluindo protocolo e NB do
  PAT quando conhecidos. Título em caixa alta e centralizado, legenda num card próprio.
  Ainda não ligado ao fluxo de "Anexar ao PAJ" (aguardando validação do usuário).

### Alterado
- Rótulos dos grupos de documentos (usados no modal "Baixar mais", em Configurações ›
  Tipos de documentos, e no novo "Levantamento SAT"): "Grupo 1 — CNIS / Declarações
  (1 clique)" → "Grupo 1 — CNIS / Declarações" (removida a informação desnecessária);
  "Grupo 2 — modais" → "Grupo 2 — Famílias de documentos"; "PAT" → "Processos
  Administrativos - Tarefas PAT" (continua sendo o último grupo).

## [1.6.2] — 2026-11-13

### Adicionado
- O NB extraído de dentro de cada documento do PAT (processo administrativo) lido e
  processado agora é gravado no log (logo após o protocolo) e exposto na lista de
  documentos do "Baixar mais" — tanto para os baixados quanto para os descartados/pulados
  (duplicata, mais de 5 anos, NB não indicado). Só é possível conhecer o NB depois que o
  documento é de fato baixado (fica dentro do PDF) — tarefas puladas ANTES do download
  (lista de ignorados, protocolo/pretensão fora do filtro) continuam sem essa informação.

## [1.6.1] — 2026-11-13

### Corrigido
- Faltava uma TERCEIRA trava automática do PAT que a correção anterior (1.6.0) não
  cobria: depois de baixado, `processar_pat` descarta o PDF se o NB extraído do PRÓPRIO
  documento não bater com o filtro de NB do PAJ — um passo separado do filtro de
  protocolo/lista de ignorados, aplicado DEPOIS do download. Agora, numa seleção
  REVISADA, esse filtro de NB também é ignorado para os itens explicitamente
  selecionados (mesmo princípio: a seleção manual é absoluta). PAJ real afetado (04410,
  3 tarefas do PAT com NB diferente do filtro) deve se resolver na próxima tentativa.

## [1.6.0] — 2026-11-13

### Corrigido
- **Seleção manual (revisada em "Baixar mais"/"Salvar seleção") não sobrepunha travas
  automáticas do PAT**: mesmo com um item do PAT explicitamente marcado e salvo, a fila
  continuava pulando-o por "espécie na lista de ignorados" ou "fora da associação de PAT
  configurada para esta pretensão" — essas duas regras (e também o filtro de NB do Grupo
  2) são aplicadas no momento do download real, sem saber que o item vinha de uma seleção
  já revisada por um humano. Agora a lista de batimento carrega consigo a marca de
  "revisado" até o download: um item explicitamente presente nela sobrepõe lista de
  ignorados do PAT, associação de PAT por pretensão e filtro de NB — a seleção manual do
  Defensor é absoluta, igual ao filtro de NB/protocolo indicado manualmente já sempre foi.
  PAJ real afetado (04410, 5 documentos pendentes) deve se resolver sozinho na próxima
  tentativa.

## [1.5.6] — 2026-11-13

### Corrigido
- Ordem dos documentos no "PAJ Completo" não batia com a do PAINEL SISDPU
  2.0: o SISDPU lista as movimentações do mais recente para o mais antigo,
  e o PDF saía nessa mesma ordem (invertida). Agora o histórico e os
  separadores/anexos saem em ordem cronológica (seq crescente, do nº 1 em
  diante) — mesmo critério de lá. Também movido o "Formulário do PAJ"
  sempre para o início da sua sequência, quando presente (mesmo critério).

## [1.5.5] — 2026-11-13

### Adicionado
- "Baixar PAJ Completo" agora checa a conexão com o SISDPU antes de
  perguntar: se já conectado, pergunta "Baixar o PAJ completo?" como antes;
  se não, pergunta "Conectar ao SISDPU para baixar o PAJ completo?" — um
  "Sim" aqui é a ordem de conectar (e, se o cofre estiver bloqueado, o modal
  de senha-mestra aparece na sequência) antes de gerar o PDF.

## [1.5.4] — 2026-11-13

### Alterado
- Tooltip do ícone "Baixar PAJ Completo" simplificado para "Baixar PAJ
  completo" (antes trazia a explicação inteira).
- Tooltip do ícone "+" (CPFs extras): "Incluir novo CPF de consulta para
  este PAJ (instituidor, assistido ou terceiro)".

## [1.5.3] — 2026-11-13

### Adicionado
- Confirmação ("Baixar o PAJ completo? Sim/Cancelar") antes de gerar o PDF
  completo — evita disparar a geração (que navega o SISDPU de verdade) por
  clique acidental no ícone.

### Alterado
- Cabeçalho: "Início" virou um ícone de casa e "Configurações" virou um
  ícone de engrenagem — visual mais limpo (nome continua no `title`, ao
  passar o mouse).

## [1.5.2] — 2026-11-13

### Corrigido
- Ícone "Baixar PAJ Completo" aparecia apagado/não clicável — tinha um
  `:disabled` vinculado ao estado de "gerando" que, por algum motivo do
  Alpine.js, travava em `true` para o ícone (diferente do ícone de copiar,
  que nunca teve essa trava). Removido — a proteção contra duplo-clique já
  é feita dentro da própria função, sem precisar do atributo. Visual agora
  idêntico ao ícone de copiar.

## [1.5.1] — 2026-11-13

### Alterado
- Ícone "Baixar PAJ Completo" movido da coluna Ação para a coluna PAJ (ao
  lado do ícone de copiar número), discreto como os demais ícones dessa
  coluna — mesma função, só mudou de lugar. Aplicado também na aba
  Individual (onde o ícone ainda não existia).
- Coluna "Ação" alinhada à esquerda (cabeçalho e linhas) na Fila Automática
  e na aba Individual.

### Adicionado
- Clique no cabeçalho "Status" da Fila Automática agrupa os PAJs pelo mesmo
  status (ordem crescente); clique de novo volta à ordem original. Não
  afeta a seleção da fila nem nenhum outro dado — é só a ordem de exibição.

## [1.5.0] — 2026-11-13

### Adicionado
- **"Baixar PAJ Completo"**: novo ícone na coluna Ação da Fila Automática — gera,
  na hora (sem precisar sincronizar antes), um único PDF com capa (PAJ,
  assistido, CPF, pretensão, data de abertura), narrativa, histórico de
  movimentações e TODOS os anexos de cada movimentação, direto do SISDPU —
  sem precisar abrir o navegador nem risco de derrubar a sessão do painel por
  um login concorrente. Sempre completo, sem opção de recorte/arquivos crus.
  Tempo esperado: ~7s para abrir o PAJ + ~11s por movimentação que tiver
  anexo (custo do próprio SISDPU) — poucos segundos a pouco mais de um
  minuto, dependendo do PAJ.

## [1.4.6] — 2026-11-13

### Corrigido
- **"Múltiplos Requerimentos" travava mesmo depois da revisão manual**: salvar a seleção
  em "Baixar mais" (mesmo mantendo tudo marcado, por decisão consciente) não bastava para
  a fila parar de pausar o PAJ — ela recalculava o limite do zero a cada rodada e pausava
  de novo sempre que o grupo continuasse acima dele. Agora a lista de batimento grava uma
  marca "revisado" sempre que "Baixar mais"/"Salvar seleção" é usado; com essa marca, a
  fila para de pausar por esse motivo e passa a baixar normalmente o que estiver marcado,
  mesmo que o grupo continue acima do limite.

### Adicionado
- Selo "Múltiplos Requerimentos" ganha um "✓" na frente quando o PAJ já foi revisado e
  salvo dessa forma — distingue visualmente quem ainda precisa de revisão de quem já foi
  conferido e está liberado para a fila baixar.

## [1.4.5] — 2026-11-13

### Alterado
- Log da Fila Automática: removida a linha separadora ("====...") antes do cabeçalho de
  cada PAJ — ficou pesada demais. Mantido só o cabeçalho destacado.

## [1.4.4] — 2026-11-13

### Adicionado
- Ícone do painel (favicon da aba e logo no cabeçalho): escudo da GERID, recortado da logo
  oficial, com o fundo transparente.

### Alterado
- Nome da aba do navegador: "Painel SAT" (antes "Arquivos SAT Central"/"Painel SAT
  Central").
- Título do cabeçalho: "PAINEL DE DOWNLOADS - SAT CENTRAL" (antes "Painel SAT Central",
  com um ícone de documento genérico).
- Removido o cabeçalho "Arquivos SAT Central (N)" da página inicial — ficou só a lista
  "Esta aplicação permite...".

## [1.4.3] — 2026-11-13

### Alterado
- Configurações › Parâmetros dividida em 4 sub-abas, para não precisar rolar uma lista
  longa de seções para achar a que importa: **Preferências** (SISDPU + Documentos),
  **Configuração Antibloqueio** (Intervalos de Consultas + CAPTCHA), **Tipos de documentos
  por pretensão**, **Processos Administrativos a ignorar**. Nenhum campo ou comportamento
  mudou — só a organização visual.

## [1.4.2] — 2026-11-13

### Corrigido
- **Log de download marcado "completo" mesmo quando a rotina foi interrompida**: bastava a
  consulta do CPF ter sido feita para o log final gravar `parcial: False` — mesmo que uma
  exceção (ex.: perda de conexão com a janela do SAT) tivesse interrompido o meio do
  caminho, logo depois de começar. Isso fez um PAJ real (sem lista de batimento ainda)
  aparecer "Pronto para anexar" com **zero documentos baixados**, sem nenhuma falha visível
  em lugar nenhum. Agora o log só é marcado "completo" quando a rotina chega ao fim
  sozinha; qualquer interrupção mantém `parcial: True`, e o painel volta a reconhecer isso
  como pendente de nova tentativa. PAJ real afetado (04410) corrigido.

## [1.4.1] — 2026-11-13

### Corrigido
- **Regressão de "baixado"**: quando um tipo de documento já estava completo e uma rodada
  seguinte não o selecionava mais (por não ter mais nada pendente dele), a mesclagem do
  resultado sobrescrevia esse status de volta para "não selecionado" — fazendo a lista de
  batimento achar, para sempre, que ainda faltava baixar algo que já estava pronto no
  disco (dois PAJs reais reportaram "download incompleto" sem nenhum documento com falha
  de verdade). Um item `baixado` agora nunca regride.
- **"Sem dados" sem fim**: quando o SAT confirmava, na tentativa real de download, que um
  documento específico não existe (diferente do "sem dados" já tratado no levantamento),
  a lista de batimento continuava marcando-o como pendente para sempre, fazendo a fila
  reconsultar o SAT a cada rodada por algo que nunca vai existir. Agora esse resultado é
  tratado como definitivo (`incluir` vira False) — não é falha, `falha`/`antirobo`/
  `indisponivel` continuam retentáveis normalmente.
- Mensagem do log "não baixado (desativado para esta pretensão)" sempre culpava a
  pretensão, mesmo quando a causa real era outra (já baixado, ou fora do filtro de NB/
  protocolo) — confundiu o usuário a conferir a configuração da pretensão à toa. Agora diz
  "nada pendente deste tipo nesta rodada", que cobre os três motivos reais sem acusar o
  errado.

### Adicionado
- Log da Fila Automática: início do trabalho de cada PAJ agora vem com uma linha
  separadora ("====...") + o cabeçalho, as duas com fundo contrastante — fácil de achar
  onde cada PAJ começa, rolando o log.

## [1.4.0] — 2026-11-13

### Adicionado
- **"Múltiplos Requerimentos"**: novo status na Fila Automática. Quando um grupo de
  documentos (Carta de Concessão, CRER, Avaliação Social, Laudo Médico, Tarefas PAT,
  Histórico de Empréstimos Consignados ou qualquer outro tipo que apareça em
  grupo/tabela — nunca os de documento único, como CNIS) tem mais itens pendentes do que
  o limite configurado, a fila pausa esse PAJ (sem gastar nenhuma consulta no SAT) em vez
  de baixar tudo automaticamente, e recomenda revisão manual via "Baixar mais". O PAJ sai
  sozinho desse estado assim que a seleção cair dentro do limite — não é uma marca fixa,
  é recalculada na hora a partir da lista de batimento. Limite configurável em
  Configurações › Parâmetros (padrão: 5).
- Ícone "Baixar mais" passou a aparecer sempre que houver levantamento/lista de batimento
  — mesmo sem nenhum documento baixado ainda (antes exigia pelo menos 1 download).
- Log da Fila Automática: as linhas de início de cada etapa (levantamento × download) têm
  cor de fonte e linha separadora próprias, para não se confundir com o resto do log.
- Ícone de copiar ao lado do número do PAJ (igual ao que já existia para o CPF).
- Checkbox por PAJ (e "marcar todos" no cabeçalho) na tabela da Fila Automática: só os
  marcados entram na próxima rodada da fila automática — escolha sempre reversível,
  persistida, não afeta nada já baixado. PAJ sem marcação explícita conta como marcado
  (comportamento de sempre).
- Ao clicar em "Iniciar download", o painel do log abre sozinho (antes era preciso clicar
  em "Ver Log" manualmente); o botão de abrir/ocultar ("Ver Log"/"Ocultar Log") passou
  para o canto direito da linha. O log também rola sozinho até a última linha a cada
  atualização, sem precisar arrastar a barra de rolagem.

### Corrigido
- Progresso perdido quando a conexão com a janela do SAT caía no meio de um download:
  documentos já baixados com sucesso (CNIS, CRER, Laudo Médico etc.) nunca eram mesclados
  na lista de batimento se uma exceção interrompesse o processo depois — a próxima rodada
  baixava tudo de novo à toa. As tarefas do PAT tinham um problema adicional: só entravam
  no inventário depois que TODA a paginação terminasse (a deduplicação por NB precisa ver
  tudo de uma vez); se a conexão caísse no meio da paginação, os PDFs já baixados ficavam
  esquecidos numa pasta temporária, nunca processados. Os dois casos agora preservam o
  progresso parcial antes de repropagar o erro.
- Depois que a janela do SAT caía de vez, a fila insistia nos PAJs seguintes, cada um
  falhando imediatamente com o mesmo erro técnico — agora reconhece esse padrão e para a
  rodada inteira com um aviso único e claro.

## [1.3.0] — 2026-11-12

### Adicionado
- **Lista de batimento**: na 1ª rodada de cada PAJ (fila automática e CPFs extras), o
  painel agora roda um levantamento completo no SAT (independente da pretensão) antes de
  baixar qualquer coisa, e monta uma lista persistente com todos os documentos
  encontrados. Da 2ª rodada em diante, só essa lista manda o que buscar — pretensão e
  filtros nunca são reconsultados depois da 1ª vez.
- Modal "Baixar mais documentos": marcar/desmarcar qualquer item ainda não baixado é
  sempre reversível (nunca um bloqueio permanente); novo botão **"Salvar seleção"** grava
  a escolha para a fila buscar na próxima rodada, sem disparar download na hora.
- O texto entre parênteses de um documento "sem dados"/"indisponível" no levantamento
  agora mostra a mensagem **exata** relatada pelo SAT, além do status genérico.
- Novo status **"Saiu da {caixa}"**: um PAJ que estava sincronizado mas saiu da caixa de
  grupo não é mais descartado da lista — fica marcado, com "Anexar ao PAJ" (pulando só a
  conclusão do trâmite original) e um ícone de lixeira (remove da lista e apaga os
  arquivos já baixados) disponíveis.
- Pausa fixa de 10s (em vez do ritmo normal de 30-45s) ao abrir um grupo (Laudo
  Social/CCON/CRER/Consignados) antes de baixar o 1º item da lista.
- Na 2ª ativação seguida da proteção antirrobô no mesmo documento, a pausa antes da
  última tentativa é dobrada.

### Corrigido
- A fila automática pulava o PAJ inteiro quando o assistido principal não tinha CPF,
  mesmo havendo um CPF extra (terceiro/instituidor) com CPF cadastrado — agora só pula a
  etapa do assistido principal e segue para os CPFs extras.
- Duas frases do SAT para "documento não existe" (Laudo Social) eram classificadas como
  falha real.
- Ícone de lixeira de um CPF extra não fazia nada ao clicar (`classeCpfExtra` chamado sem
  `this.` dentro de `removerCpfExtra`).
- Aspas retas dentro de um atributo `:title` quebravam o parsing do HTML e corrompiam o
  JavaScript da página inteira.
- Uma falha passageira do navegador ao ler a caixa de grupo no SISDPU (ex.: "Execution
  context was destroyed... navigation", quando a página estava navegando bem na hora da
  leitura) aparecia crua, sem explicação, na barra de status da Fila Automática — agora
  mostra uma mensagem compreensível (e sugere sincronizar de novo), mantendo o detalhe
  técnico ao final.
- "Iniciar download" não fazia nada, sem nenhum aviso, quando a janela do SAT estava
  fechada/indisponível: o backend deixava a exceção de conexão (CDP) subir crua até virar
  um HTTP 500 sem corpo JSON, e o front-end não tinha tratamento para essa resposta.
  Agora o backend devolve um aviso claro e o front-end sempre mostra alguma mensagem,
  mesmo numa falha inesperada.
- Detecção de "Saiu da {caixa}" marcava TODOS os PAJs de uma vez quando uma única
  sincronização vinha vazia/incompleta (soluço passageiro do SISDPU, mesmo reportando
  sucesso). Agora exige 2 sincronizações seguidas sem o PAJ aparecer antes de confirmar;
  se ele reaparecer entre uma e outra, a suspeita é descartada.

### Alterado
- Rótulo "dispensado pela pretensão" → "Download não previsto para essa pretensão".
- Coluna "Filtro (NB/Protocolo)" → apenas "Filtro" (tooltip já explica).
- Selo "Saiu da {caixa}" em vermelho; célula da coluna de status com `whitespace-nowrap`
  para não empurrar o ícone de documentos para a 2ª linha.
- Barra de status da Fila Automática simplificada: removido o badge "parada/rodando"
  (redundante — já visível pela troca dos botões Iniciar/Parar); "N pendência(s) na
  caixa" removido (redundante com os badges de status de cada PAJ); "sincronizado em"
  ficou junto do botão "Sincronizar caixa"; "processando: {PAJ}" ficou junto do botão
  "Parar"; a linha de erro ficou sozinha, centralizada. Botão "Iniciar fila automática" →
  "Iniciar download".
- "Sincronizar caixa" bem-sucedida: o texto "sincronizado em ..." fica verde com um ✓ na
  frente por 5s, depois volta ao normal.
- Formato de data (histórico, "sincronizado em", anexação complementar) de DD-MM-AAAA
  para DD/MM/AAAA.
