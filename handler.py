import json
import os
import uuid
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import boto3
import requests
import firebase_admin
from firebase_admin import auth, credentials

# Setup logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# AWS resources
dynamodb         = boto3.resource('dynamodb')
table_inscricoes = dynamodb.Table('Inscricoes')
table_interesse  = dynamodb.Table('ListaInteresse')
table_cursos     = dynamodb.Table('Cursos')
table_descontos  = dynamodb.Table('Descontos')
ses              = boto3.client('ses')
s3               = boto3.client('s3')

# Configs
ASAAS_API_KEY     = os.environ.get('ASAAS')
ASAAS_ENDPOINT    = "https://www.asaas.com/api/v3"
REMETENTE         = 'programa AI <no-reply@programaai.dev>'
FIREBASE_BUCKET   = os.environ.get('FIREBASE_BUCKET')
FIREBASE_KEY_PATH = os.environ.get('FIREBASE_KEY_PATH')
ADMIN_EMAIL       = os.environ.get('ADMIN_EMAIL')

FULLSTACK_NOME_CURSO = "Curso Presencial Programação Fullstack"


def init_firebase():
    if not firebase_admin._apps:
        obj = s3.get_object(Bucket=FIREBASE_BUCKET, Key=FIREBASE_KEY_PATH)
        key = json.load(obj['Body'])
        cred = credentials.Certificate(key)
        firebase_admin.initialize_app(cred)

init_firebase()


def salvar_inscricao(event, context):
    path   = event.get("path", "")
    method = event.get("httpMethod", "")
    qs     = event.get("queryStringParameters") or {}
    logger.info("Incoming request: path=%s method=%s qs=%s", path, method, qs)

    # GET /pagamento-info?inscricaoId=...
    if path.endswith("/pagamento-info") and method == "GET":
        iid = (qs.get("inscricaoId") or "").strip()
        if not iid:
            return resposta(400, {"error": "Parâmetro 'inscricaoId' é obrigatório."})
        try:
            info = montar_pagamento_info(iid)
            return resposta(200, info)
        except ValueError as ve:
            logger.warning("Pagamento-info inválido: %s", ve)
            return resposta(400, {"error": str(ve)})
        except Exception:
            logger.exception("Erro ao montar pagamento-info")
            return resposta(500, {"error": "Erro interno ao montar pagamento-info"})


    # POST /isAssinatura
    if path.endswith("/isAssinatura") and method == "POST":
        logger.info("isAssinatura request body: %s", event.get("body"))
        try:
            body = json.loads(event.get("body", "{}"))
            iid = (body.get("inscricaoId") or "").strip()
            valor_assinatura = bool(body.get("isAssinatura", True))

            if not iid:
                logger.warning("inscricaoId ausente em /isAssinatura")
                return resposta(400, {"error": "Parâmetro 'inscricaoId' é obrigatório."})

            # busca inscrição
            resp = table_inscricoes.get_item(Key={"id": iid})
            insc = resp.get("Item")
            if not insc:
                logger.warning("Inscrição %s não encontrada em /isAssinatura", iid)
                return resposta(404, {"error": f"Inscrição '{iid}' não encontrada"})

            # valida curso
            nome_curso = insc.get("curso", "")
            if FULLSTACK_NOME_CURSO not in nome_curso:
                logger.info("Bloqueado /isAssinatura: curso '%s' não contém '%s'", nome_curso, FULLSTACK_NOME_CURSO)
                return resposta(403, {"error": f"Ação permitida apenas para inscrições do curso que contenha '{FULLSTACK_NOME_CURSO}'."})

            # atualiza campo
            agora = datetime.now(timezone(timedelta(hours=-3))).isoformat()
            upd = table_inscricoes.update_item(
                Key={"id": iid},
                UpdateExpression="SET isAssinatura = :v, updatedAt = :u",
                ExpressionAttributeValues={
                    ":v": valor_assinatura,
                    ":u": agora
                },
                ReturnValues="ALL_NEW"
            )
            item_atualizado = upd.get("Attributes", {})
            logger.info("Inscrição %s atualizada isAssinatura=%s", iid, valor_assinatura)

            # envia e-mail para admin
            try:
                enviar_email_admin_is_assinatura(item_atualizado)
            except Exception:
                logger.exception("Erro ao enviar email de solicitação de assinatura")

            return resposta(200, {
                "message": "Campo isAssinatura atualizado com sucesso.",
                "inscricaoId": iid,
                "isAssinatura": item_atualizado.get("isAssinatura", valor_assinatura)
            })

        except Exception:
            logger.exception("Erro no endpoint /isAssinatura")
            return resposta(500, {"error": "Erro interno ao atualizar isAssinatura"})


    # POST /clube/interesse
    if path.endswith("/clube/interesse") and method == "POST":
        body = json.loads(event.get("body", "{}"))
        logger.info("Clube Interesse POST payload: %s", body)
        if body.get("website"):
            logger.warning("Honeypot triggered for clube/interesse")
            return resposta(400, {"error":"Solicitação inválida."})
        nome, email, aceita = body.get("nome"), body.get("email"), body.get("aceitaContato")
        if not nome or not email or not aceita:
            logger.warning("Missing required fields in clube/interesse")
            return resposta(400, {"error":"Nome, email e aceitaContato são obrigatórios."})
        if verificar_interesse_existente(email):
            logger.info("Email %s já cadastrado no clube", email)
            return resposta(409, {"error":f"Email {email} já cadastrado."})
        item = {
            "id": str(uuid.uuid4()),
            "nome": nome,
            "email": email,
            "whatsapp": body.get("whatsapp",""),
            "interesse": body.get("interesses",[]),
            "aceita_contato": aceita,
            "dataCadastro": datetime.now(timezone(timedelta(hours=-3))).isoformat()
        }
        table_interesse.put_item(Item=item)
        logger.info("Novo membro do clube salvo: %s", item)
        try:
            enviar_email_boas_vindas_clube(item)
            enviar_email_admin_clube(item)
        except Exception:
            logger.exception("Erro enviando e-mails clube")
        return resposta(201, {"message":"Cadastro no Clube realizado."})

    # GET /clube/interesse?email=...
    if path.endswith("/clube/interesse") and method == "GET":
        email = qs.get("email","").strip()
        logger.info("Clube Interesse GET query: email=%s", email)
        if not email:
            return resposta(400, {"error":"Parâmetro 'email' é obrigatório."})
        existe = verificar_interesse_existente(email)
        logger.info("Clube check for %s: %s", email, existe)
        return resposta(200, {"existe": existe})

    # GET /checa-cupom?cupom=XXX&curso=YYY
    if path.endswith("/checa-cupom") and method == "GET":
        cupom = qs.get("cupom","").strip().upper()
        curso = qs.get("curso","").strip()
        logger.info("Checagem de cupom: cupom=%s curso=%s", cupom, curso)
        if not cupom or not curso:
            return resposta(400, {"error":"Parâmetros 'cupom' e 'curso' são obrigatórios."})

        resp = table_descontos.scan(
            FilterExpression="cupom = :c AND curso = :u AND ativo = :t AND disponivel = :t",
            ExpressionAttributeValues={":c":cupom, ":u":curso, ":t":True}
        )
        items = resp.get("Items", [])
        valid = bool(items)

        # pega o valor do desconto (ex: "10%" ou "R$10,00") se existir
        desconto = items[0]["desconto"] if valid else None
        logger.info("Cupom %s válido? %s desconto=%s", cupom, valid, desconto)

        return resposta(200, {
            "valid": valid,
            "desconto": desconto
        })

    # POST /paymentlink
    if path.endswith("/paymentlink") and method == "POST":
        logger.info("PaymentLink request body: %s", event.get("body"))
        try:
            body = json.loads(event.get("body", "{}"))
            iid = body.get("inscricaoId", "").strip()
            pm = body.get("paymentMethod", "PIX").upper()
            if not iid or pm not in ("PIX", "CARTAO"):
                logger.warning("Invalid paymentlink parameters: %s", body)
                return resposta(400, {"error": "inscricaoId e paymentMethod válidos são obrigatórios."})

            # Busca inscrição e pega valorCurso
            resp = table_inscricoes.get_item(Key={"id": iid})
            insc = resp.get("Item")
            if not insc:
                logger.warning("Inscrição %s não encontrada", iid)
                return resposta(404, {"error": f"Inscrição '{iid}' não encontrada"})

            aluno = insc.get("nomeCompleto", "")
            curso = insc.get("curso", "")
            # Aqui pegamos o valor já calculado e armazenado na inscrição:
            valor_decimal = insc.get("valorCurso", 0)
            # Se vier como Decimal, converte para float:
            valor = float(valor_decimal) if isinstance(valor_decimal, (Decimal,)) else float(valor_decimal)

            logger.info("Found inscrição %s: aluno=%s, curso=%s, valor=%s", iid, aluno, curso, valor)
            link = criar_paymentlink_asaas(curso, aluno, valor, pm, iid)
            asaas_resp = link.get("asaas", {})  # novo formato

            logger.info("Asaas link created: %s", asaas_resp.get("url"))
            return resposta(200, {
                "inscricaoId": iid,
                "paymentMethod": pm,
                "descontoExtraPix": link.get("descontoExtraPix", 0.0),
                "valorFinal": link.get("valorFinal"),
                "paymentLinkId": asaas_resp.get("id"),
                "url": asaas_resp.get("url")
            })

        except Exception:
            logger.exception("Erro ao gerar paymentlink")
            return resposta(500, {"error": "Erro interno ao gerar paymentlink"})

    # GET /cursos or GET /cursos?id=...
    if path.endswith("/cursos") and method == "GET":
        cid = qs.get("id")
        logger.info("Listar cursos, id=%s", cid)
        try:
            if cid:
                item = table_cursos.get_item(Key={"id": cid}).get("Item")
                if not item:
                    logger.warning("Curso %s não encontrado", cid)
                    return resposta(404, {"error":f"Curso '{cid}' não encontrado"})
                return resposta(200, item)
            else:
                items = table_cursos.scan().get("Items",[])
                logger.info("Total cursos retornados: %d", len(items))
                return resposta(200, items)
        except Exception:
            logger.exception("Erro listando cursos")
            return resposta(500, {"error":"Falha ao buscar cursos"})

    # POST /inscricao
    if path.endswith("/inscricao") and method == "POST":
        return processar_inscricao(event, context)

    # Admin routes
    """if "/galaxy" in path:
        try:
            hdr = event["headers"].get("Authorization","")
            uid, email = validar_jwt(hdr)
            logger.info("Admin auth OK: uid=%s email=%s", uid, email)
        except Exception:
            logger.warning("Admin auth failed")
            return resposta(401, {"error":"Unauthorized"})
        if path.endswith("/galaxy/inscricoes") and method == "GET":
            return listar_inscricoes()
        if path.startswith("/galaxy/inscricoes/") and method == "DELETE":
            iid = path.split("/")[-1]
            return remover_inscricao(iid)
        return resposta(404, {"error":"Admin route not found"})"""

    # CORS
    if method == "OPTIONS":
        return resposta(200, {"message":"CORS OK"})

    logger.warning("Route not found: %s %s", method, path)
    return resposta(404, {"error":"Route not found"})


def processar_inscricao(event, context):
    logger.info("Processando inscrição, body=%s", event.get("body"))
    body_raw = event.get("body")
    if not body_raw:
        return resposta(400, {"error": "Body is required"})
    body = json.loads(body_raw)
    if body.get("website"):
        logger.warning("Honeypot triggered in inscrição")
        return resposta(400, {"error": "Solicitação inválida."})

    # Extrair campos
    cpf_aluno     = body.get("cpf", "").strip()
    nome_curso    = body.get("curso", "").strip()
    nome_aluno    = body.get("nomeCompleto", "").strip()
    rg_aluno      = body.get("rg", "").strip()
    email         = body.get("email", "").strip()
    whatsapp      = body.get("whatsapp", "").strip()
    sexo          = body.get("sexo", "").strip()
    data_nasc     = body.get("dataNascimento", "").strip()
    form_ti       = body.get("formacaoTI", "").strip()
    onde_estuda   = body.get("ondeEstuda", "").strip()
    como_soube    = body.get("comoSoube", "").strip()
    nome_amigo    = body.get("nomeAmigo", "").strip()
    aceita_termos = bool(body.get("aceitouTermos"))
    cupom         = body.get("cupom", "").strip().upper()

    # Verifica duplicidade
    if verificar_inscricao_existente(cpf_aluno, nome_curso):
        logger.info("Inscrição duplicada: cpf=%s curso=%s", cpf_aluno, nome_curso)
        return resposta(409, {"error": f"Aluno {cpf_aluno} já inscrito em {nome_curso}."})

    # Busca dados do curso (para preço e para checar 'ativo')
    scan_resp = table_cursos.scan(
        FilterExpression="title = :t",
        ExpressionAttributeValues={":t": nome_curso}
    )
    items = scan_resp.get("Items", [])
    if not items:
        logger.warning("Curso '%s' não encontrado em inscrição", nome_curso)
        return resposta(404, {"error": f"Curso '{nome_curso}' não encontrado"})
    curso_item = items[0]

    # Bloqueia inscrição se curso estiver inativo
    if not curso_item.get("ativo", True):
        logger.info("Tentativa de inscrição em curso inativo: %s", nome_curso)
        return resposta(400, {
            "error": f"Inscrições para o curso '{nome_curso}' estão encerradas."
        })

    # Calcula preço original
    raw_price   = curso_item.get("price", "")
    clean_price = raw_price.replace("R$", "").replace(".", "").replace(",", ".").strip()
    try:
        valor_original = Decimal(clean_price)
    except Exception:
        logger.error("Preço inválido no curso: %s", raw_price)
        return resposta(500, {"error": f"Preço inválido: {raw_price}"})
    logger.info("Preço original do curso '%s': %s", nome_curso, valor_original)

    # Tenta aplicar desconto de cupom (se houver), mas não bloqueia inscrição se inválido
    desconto_valor = Decimal("0")
    if cupom:
        try:
            desconto = checa_cupom_e_retorna_desconto(cupom, nome_curso)
            if desconto:
                if desconto.endswith("%"):
                    pct = Decimal(desconto.rstrip("%")) / Decimal("100")
                    desconto_valor = (valor_original * pct).quantize(Decimal("0.01"))
                else:
                    val = desconto.replace("R$", "").replace(",", ".").strip()
                    desconto_valor = Decimal(val).quantize(Decimal("0.01"))
                logger.info("Desconto aplicado: %s => %s", desconto, desconto_valor)
            else:
                logger.info("Cupom '%s' inválido para o curso '%s', prosseguindo sem desconto", cupom, nome_curso)
        except Exception as e:
            logger.warning("Erro ao verificar cupom '%s': %s. Prosseguindo sem desconto.", cupom, e)

    valor_com_desconto = (valor_original - desconto_valor).quantize(Decimal("0.01"))
    logger.info("Valor com desconto final (ou preço cheio): %s", valor_com_desconto)

    # Monta e salva o item de inscrição
    inscricao_id = str(uuid.uuid4())
    now = datetime.now(timezone(timedelta(hours=-3))).isoformat()
    ip = event.get("requestContext", {}).get("identity", {}).get("sourceIp", "")
    ua = event.get("headers", {}).get("User-Agent", "")
    item = {
        "id": inscricao_id,
        "curso": nome_curso,
        "nomeCompleto": nome_aluno,
        "cpf": cpf_aluno,
        "rg": rg_aluno,
        "email": email,
        "whatsapp": whatsapp,
        "sexo": sexo,
        "dataNascimento": data_nasc,
        "formacaoTI": form_ti,
        "ondeEstuda": onde_estuda,
        "comoSoube": como_soube,
        "nomeAmigo": nome_amigo,
        "aceitouTermos": aceita_termos,
        "dataInscricao": now,
        "ip": ip,
        "userAgent": ua,
        "valorOriginal": valor_original,
        "valorCurso": valor_com_desconto,
        "cupom": cupom or None
    }
    table_inscricoes.put_item(Item=item)
    logger.info("Inscrição salva: %s", item)

    # Envia notificações
    try:
        enviar_email_para_aluno(item)
        enviar_email_para_admin(item)
    except Exception:
        logger.exception("Erro ao enviar e-mails de inscrição")

    return resposta(201, {
        "message": "Inscrição criada com sucesso!",
        "inscricao_id": inscricao_id
    })


def checa_cupom_e_retorna_desconto(cupom, curso):
    resp = table_descontos.scan(
        FilterExpression="cupom = :c AND curso = :s",
        ExpressionAttributeValues={":c":cupom, ":s":curso}
    )
    items = resp.get("Items", [])
    return items[0].get("desconto") if items else None


def criar_paymentlink_asaas(curso, aluno, valor, metodo, ext_ref):
    """
    Cria PaymentLink no Asaas aplicando, quando cabível:
    - Desconto extra PIX de R$150 para cursos Fullstack.

    Retorno:
      {
        "asaas": <resposta JSON do Asaas>,
        "valorFinal": <float>,
        "descontoExtraPix": <float>
      }
    """
    # Normaliza valor de entrada para Decimal (seguro p/ cálculo)
    valor_dec = Decimal(str(valor)).quantize(Decimal("0.01"))

    # Regras de desconto
    desconto_extra = Decimal("0.00")
    if metodo == "PIX" and FULLSTACK_NOME_CURSO in curso:
        desconto_extra = Decimal("150.00")
        valor_dec = (valor_dec - desconto_extra).quantize(Decimal("0.01"))
        # Evita valor zero/negativo no Asaas
        if valor_dec <= Decimal("0.00"):
            valor_dec = Decimal("0.01")

    hdr = {"Content-Type": "application/json", "access_token": ASAAS_API_KEY}
    nome = f"Inscrição: {curso}"
    desc = f"{nome}. Aluno: {aluno}"

    if metodo == "PIX":
        payload = {
            "name": nome,
            "billingType": "PIX",
            "chargeType": "DETACHED",
            "value": float(valor_dec),            # valor já com desconto (se houver)
            "description": desc,
            "dueDateLimitDays": 2,
            "externalReference": ext_ref,
            "notificationEnabled": True
        }
    else:
        # Cartão segue a regra atual (acréscimo de 8% sobre o valor base sem desconto PIX)
        tc = round(float(valor_dec) * 1.08, 2)
        payload = {
            "name": nome,
            "billingType": "CREDIT_CARD",
            "chargeType": "INSTALLMENT",
            "value": tc,
            "description": desc,
            "dueDateLimitDays": 7,
            "maxInstallmentCount": 12,
            "externalReference": ext_ref,
            "notificationEnabled": True
        }

    logger.info("Asaas payload: %s", payload)
    resp = requests.post(f"{ASAAS_ENDPOINT}/paymentLinks", headers=hdr, json=payload)
    resp.raise_for_status()
    result = resp.json()
    logger.info("Asaas response: %s", result)

    return {
        "asaas": result,
        "valorFinal": float(valor_dec),
        "descontoExtraPix": float(desconto_extra) if desconto_extra > 0 else 0.0
    }


def enviar_email_boas_vindas_clube(item):
    assunto = "🎉 Bem-vindo ao Clube programa AI!"
    html = f"<h2>Parabéns {item['nome']}!</h2><p>Você entrou no Clube!</p>"
    ses.send_email(Source=REMETENTE,
                   Destination={"ToAddresses":[item["email"]]},
                   Message={"Subject":{"Data":assunto},
                            "Body":{"Html":{"Data":html}}})
    logger.info("Email boas-vindas clube enviado a %s", item["email"])


def enviar_email_admin_clube(item):
    assunto = f"Novo membro do Clube: {item['nome']}"
    html = f"<p>Email: {item['email']}</p>"
    ses.send_email(Source=REMETENTE,
                   Destination={"ToAddresses":[ADMIN_EMAIL]},
                   Message={"Subject":{"Data":assunto},
                            "Body":{"Html":{"Data":html}}})
    logger.info("Email admin clube enviado")


def enviar_email_para_aluno(insc):
    inscricao_id = insc["id"]
    curso = insc["curso"]
    nome = insc["nomeCompleto"]
    pagamento_url = f"https://www.programaai.dev/pagamento/{inscricao_id}"

    assunto = f"Recebemos sua inscrição em {curso}"

    html = f"""
    <html>
      <body style="font-family:Arial, sans-serif; line-height:1.6; color:#333;">
        <!-- Logo -->
        <div style="text-align:center; margin-bottom:20px;">
          <img src="https://programaai.dev/assets/logo-BPg_3cKF.png"
               alt="programa AI"
               style="height:50px;" />
        </div>

        <h2 style="color:#0056b3; margin-bottom:0.5em;">
          Olá, {nome}!
        </h2>

        <p>
          Recebemos sua inscrição no curso <strong>{curso}</strong> e
          já estamos preparando tudo para você.
        </p>

        <p>
          Para garantir sua vaga, confirme seu pagamento clicando no link
          abaixo:
        </p>
        <p style="text-align:center; margin:1.5em 0;">
          <a href="{pagamento_url}"
             style="display:inline-block; padding:12px 24px; background:#28a745; color:#fff; text-decoration:none; border-radius:4px;">
            CONFIRMAR PAGAMENTO
          </a>
        </p>

        <p>
          Estamos ansiosos para começar essa jornada de muito código e
          conhecimento! 🚀
        </p>

        <p>
          ⚠️ A vaga só estará assegurada após a confirmação do pagamento.
        </p>

        <p>
          Em breve, você será adicionado ao grupo exclusivo de WhatsApp do
          curso, onde compartilharemos todas as novidades, inclusive conteúdos
          de pré-curso!
        </p>

        <p>
          Qualquer dúvida, é só responder este e-mail ou falar conosco no
          WhatsApp. Estamos aqui para ajudar! 😊
        </p>

        <hr style="border:none; border-top:1px solid #eee; margin:2em 0;" />

        <p style="font-size:0.9em; color:#777;">
          Se você não se inscreveu ou recebeu este e-mail por engano, por
          favor, ignore.
        </p>
      </body>
    </html>
    """

    ses.send_email(
        Source=REMETENTE,
        Destination={"ToAddresses": [insc["email"]]},
        Message={
            "Subject": {"Data": assunto},
            "Body": {"Html": {"Data": html}}
        }
    )
    logger.info("Email de confirmação enviado a %s", insc["email"])


def enviar_email_para_admin(insc):
    assunto = f"📥 Nova inscrição: {insc['curso']} - {insc['nomeCompleto']}"
    html = "<ul>" + "".join(
        f"<li><strong>{k}:</strong> {v}</li>"
        for k, v in insc.items()
    ) + "</ul>"
    ses.send_email(Source=REMETENTE,
                   Destination={"ToAddresses":[ADMIN_EMAIL]},
                   Message={"Subject":{"Data":assunto},
                            "Body":{"Html":{"Data":html}}})
    logger.info("Email admin inscrição enviado")

def enviar_email_admin_is_assinatura(insc):
    assunto = f"📄 Solicitação de Assinatura - {insc.get('curso', '')} - {insc.get('nomeCompleto', '')}"
    html = "<h2>Foi solicitada a assinatura para a seguinte inscrição:</h2>"
    html += "<ul>"
    for k, v in insc.items():
        html += f"<li><strong>{k}:</strong> {v}</li>"
    html += "</ul>"

    ses.send_email(
        Source=REMETENTE,
        Destination={"ToAddresses": [ADMIN_EMAIL]},
        Message={
            "Subject": {"Data": assunto},
            "Body": {"Html": {"Data": html}}
        }
    )
    logger.info("Email admin isAssinatura enviado para %s", ADMIN_EMAIL)



def verificar_inscricao_existente(cpf, curso):
    resp = table_inscricoes.scan(
        FilterExpression="cpf = :c AND curso = :u",
        ExpressionAttributeValues={":c":cpf,":u":curso}
    )
    exists = bool(resp.get("Items",[]))
    logger.info("Verifica duplicidade cpf=%s curso=%s => %s", cpf, curso, exists)
    return exists


def verificar_interesse_existente(email):
    resp = table_interesse.scan(
        FilterExpression="email = :e",
        ExpressionAttributeValues={":e":email}
    )
    exists = bool(resp.get("Items",[]))
    logger.info("Verifica interesse email=%s => %s", email, exists)
    return exists


def listar_inscricoes():
    items = table_inscricoes.scan().get("Items",[])
    logger.info("Listagem de inscricoes, total=%d", len(items))
    return resposta(200, items)


def remover_inscricao(iid):
    table_inscricoes.delete_item(Key={"id":iid})
    logger.info("Inscrição removida: %s", iid)
    return resposta(200, {"message":"Inscrição removida"})

def montar_pagamento_info(inscricao_id: str) -> dict:
    """
    Monta o payload de informações de pagamento para a página de Pagamento.
      - PIX: valor base; se curso Fullstack, aplica desconto extra de R$150,00
      - CARTÃO: valor base * 1.08; exibe 'até 12x de ...'
      - FULLSTACK: exibe plano de 6 mensalidades de R$250,00
    Retorna números e também strings formatadas (BRL) para o front.
    """
    # 1) Busca inscrição
    resp_insc = table_inscricoes.get_item(Key={"id": inscricao_id})
    insc = resp_insc.get("Item")
    if not insc:
        raise ValueError(f"Inscrição '{inscricao_id}' não encontrada.")

    curso_title = insc.get("curso", "").strip()
    if not curso_title:
        raise ValueError("Título do curso ausente na inscrição.")

    # 2) Busca curso por título
    resp_curso = table_cursos.scan(
        FilterExpression="title = :t",
        ExpressionAttributeValues={":t": curso_title}
    )
    cursos = resp_curso.get("Items", [])
    if not cursos:
        raise ValueError(f"Curso '{curso_title}' não encontrado.")
    curso_item = cursos[0]

    # 3) Extrai preço base do curso (string tipo 'R$1499,99')
    raw_price = (curso_item.get("price") or "").strip()
    if not raw_price:
        raise ValueError(f"Preço não definido para o curso '{curso_title}'.")

    clean_price = raw_price.replace("R$", "").replace(".", "").replace(",", ".").strip()
    try:
        base = Decimal(clean_price).quantize(Decimal("0.01"))
    except Exception:
        raise ValueError(f"Preço inválido do curso: {raw_price}")

    is_fullstack = FULLSTACK_NOME_CURSO in curso_title

    # 4) Regra PIX
    desconto_pix = Decimal("150.00") if is_fullstack else Decimal("0.00")
    pix_valor = (base - desconto_pix).quantize(Decimal("0.01"))
    if pix_valor <= Decimal("0.00"):
        pix_valor = Decimal("0.01")

    # 5) Regra CARTÃO (8%)
    cartao_valor = (base * Decimal("1.08")).quantize(Decimal("0.01"))
    cartao_12x = (cartao_valor / Decimal("12")).quantize(Decimal("0.01"))

    # 6) Plano Mensalidades (somente Fullstack)
    mensalidades_info = {
        "disponivel": False,
        "parcelas": 0,
        "valorParcela": 0.0,
        "valorParcelaFmt": "",
        "mensagem": ""
    }
    if is_fullstack:
        mensalidades_info = {
            "disponivel": True,
            "parcelas": 6,
            "valorParcela": 250.00,
            "valorParcelaFmt": format_brl(250.00),
            "mensagem": (
                "Plano de 6 mensalidades: você recebe todo mês a cobrança de "
                f"{format_brl(250.00)} (pagamento via PIX ou boleto). Simples e previsível. 😉"
            )
        }

    # 7) Mensagens de marketing (com BRL formatado)
    if is_fullstack and desconto_pix > 0:
        msg_pix = (
            f"PIX com DESCONTO EXTRA de {format_brl(desconto_pix)} exclusivo para Fullstack. "
            f"Aproveite: de {format_brl(base)} por {format_brl(pix_valor)} no PIX! 🎉"
        )
    else:
        msg_pix = (
            f"Economize no PIX: pagamento à vista e acesso garantido. Valor: {format_brl(pix_valor)}."
        )

    msg_cartao = (
        f"No cartão: {format_brl(cartao_valor)} (já com taxas). "
        f"Parcele em até 12x de {format_brl(cartao_12x)} e comece agora mesmo! 💳🚀"
    )

    # 8) Retorno “pronto para tela” (números + strings formatadas)
    return {
        "inscricaoId": inscricao_id,
        "curso": {
            "title": curso_title,
            "ativo": bool(curso_item.get("ativo", True))
        },
        "precoBase": float(base),
        "precoBaseFmt": format_brl(base),
        "pix": {
            "valor": float(pix_valor),
            "valorFmt": format_brl(pix_valor),
            "descontoExtraAplicado": float(desconto_pix),
            "descontoExtraAplicadoFmt": format_brl(desconto_pix) if desconto_pix > 0 else "",
            "mensagem": msg_pix
        },
        "cartao": {
            "valor": float(cartao_valor),
            "valorFmt": format_brl(cartao_valor),
            "ate12x": {
                "parcelas": 12,
                "valorParcela": float(cartao_12x),
                "valorParcelaFmt": format_brl(cartao_12x)
            },
            "mensagem": msg_cartao
        },
        "mensalidades": mensalidades_info,
        "observacoesCurso": {
            "obsPrice": curso_item.get("obsPrice") or "",
            "modalidade": curso_item.get("modalidade") or "",
            "horario": curso_item.get("horario") or ""
        }
    }


def format_brl(value) -> str:
    """
    Formata número/Decimal como BRL (pt-BR), ex.: 1499.9 -> 'R$ 1.499,90'
    """
    d = Decimal(str(value)).quantize(Decimal("0.01"))
    s = f"{d:,.2f}"                 # '1,234.56'
    s = s.replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {s}"



def validar_jwt(hdr):
    if not hdr or not hdr.startswith("Bearer "):
        raise Exception("Invalid auth")
    token = hdr.split()[1]
    dec = auth.verify_id_token(token)
    logger.info("JWT validado: uid=%s email=%s", dec["uid"], dec.get("email"))
    return dec["uid"], dec.get("email")


def resposta(status, body):
    return {"statusCode": status, "headers": cors_headers(), "body": json.dumps(body)}

def cors_headers():
    return {
        "Access-Control-Allow-Origin": "https://programaai.dev",
        "Access-Control-Allow-Headers": "*",
        "Access-Control-Allow-Methods": "OPTIONS,GET,POST,DELETE",
        "Content-Type": "application/json"
    }
