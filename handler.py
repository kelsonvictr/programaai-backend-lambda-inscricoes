import json
import os
import uuid
import logging
from datetime import datetime, timedelta, timezone

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
            body = json.loads(event.get("body","{}"))
            iid  = body.get("inscricaoId","").strip()
            pm   = body.get("paymentMethod","PIX").upper()
            if not iid or pm not in ("PIX","CARTAO"):
                logger.warning("Invalid paymentlink parameters: %s", body)
                return resposta(400, {"error":"inscricaoId e paymentMethod válidos são obrigatórios."})
            resp = table_inscricoes.get_item(Key={"id": iid})
            insc = resp.get("Item")
            if not insc:
                logger.warning("Inscrição %s não encontrada", iid)
                return resposta(404, {"error":f"Inscrição '{iid}' não encontrada"})
            aluno = insc.get("nomeCompleto","")
            cpf   = insc.get("cpf","")
            curso = insc.get("curso","")
            logger.info("Found inscrição %s: aluno=%s, curso=%s", iid, aluno, curso)
            # busca curso por nome
            scan = table_cursos.scan(
                FilterExpression="title = :t",
                ExpressionAttributeValues={":t":curso}
            ).get("Items",[])
            if not scan:
                logger.warning("Curso %s não encontrado na table Cursos", curso)
                return resposta(404, {"error":f"Curso '{curso}' não encontrado"})
            raw_price = scan[0].get("price","")
            clean = raw_price.replace("R$","").replace(" ","").replace(".","").replace(",",".")
            valor = float(clean)
            logger.info("Creating Asaas link: valor=%s method=%s", valor, pm)
            link = criar_paymentlink_asaas(curso, aluno, cpf, valor, pm, str(uuid.uuid4()))
            logger.info("Asaas link created: %s", link.get("url"))
            return resposta(200, {
                "inscricaoId": iid,
                "paymentLinkId": link.get("id"),
                "url": link.get("url")
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
    if "/galaxy" in path:
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
        return resposta(404, {"error":"Admin route not found"})

    # CORS
    if method == "OPTIONS":
        return resposta(200, {"message":"CORS OK"})

    logger.warning("Route not found: %s %s", method, path)
    return resposta(404, {"error":"Route not found"})


def processar_inscricao(event, context):
    logger.info("Processando inscrição, body=%s", event.get("body"))
    body_raw = event.get("body")
    if not body_raw:
        return resposta(400, {"error":"Body is required"})
    body = json.loads(body_raw)
    if body.get("website"):
        logger.warning("Honeypot triggered in inscrição")
        return resposta(400, {"error":"Solicitação inválida."})

    # Extrair campos
    cpf_aluno   = body.get("cpf","").strip()
    nome_curso  = body.get("curso","").strip()
    nome_aluno  = body.get("nomeCompleto","").strip()
    rg_aluno    = body.get("rg","").strip()
    email       = body.get("email","").strip()
    whatsapp    = body.get("whatsapp","").strip()
    sexo        = body.get("sexo","").strip()
    data_nasc   = body.get("dataNascimento","").strip()
    form_ti     = body.get("formacaoTI","").strip()
    onde_estuda = body.get("ondeEstuda","").strip()
    como_soube  = body.get("comoSoube","").strip()
    nome_amigo  = body.get("nomeAmigo","").strip()
    aceita_termos = bool(body.get("aceitouTermos"))
    cupom       = body.get("cupom","").strip().upper()

    # Duplicidade
    if verificar_inscricao_existente(cpf_aluno, nome_curso):
        logger.info("Inscrição duplicada: cpf=%s curso=%s", cpf_aluno, nome_curso)
        return resposta(409, {"error":f"Aluno {cpf_aluno} já inscrito em {nome_curso}."})

    now = datetime.now(timezone(timedelta(hours=-3))).isoformat()
    ip = event.get("requestContext", {}).get("identity", {}).get("sourceIp","")
    ua = event.get("headers", {}).get("User-Agent","")

    # Preço original
    curso_resp = table_cursos.get_item(Key={"title": nome_curso})
    curso_item = curso_resp.get("Item")
    if not curso_item:
        logger.warning("Curso não encontrado em inscrição: %s", nome_curso)
        return resposta(404, {"error":f"Curso '{nome_curso}' não encontrado"})
    raw_price = curso_item.get("price","")
    clean_price = raw_price.replace("R$","").replace(".","").replace(",",".").strip()
    try:
        valor_original = float(clean_price)
    except ValueError:
        logger.error("Preço inválido no curso: %s", raw_price)
        return resposta(500, {"error":f"Preço inválido: {raw_price}"})
    logger.info("Preço original do curso '%s': %f", nome_curso, valor_original)

    # Desconto
    desconto_valor = 0.0
    if cupom:
        desconto = checa_cupom_e_retorna_desconto(cupom, nome_curso)
        if not desconto:
            logger.info("Cupom invalido: %s", cupom)
            return resposta(400, {"error":"Cupom inválido ou não aplicável"})
        if desconto.endswith("%"):
            pct = float(desconto.rstrip("%"))
            desconto_valor = valor_original * pct/100
        else:
            desconto_valor = float(desconto.replace("R$","").replace(",","."))
        logger.info("Desconto aplicado: %s => %f", desconto, desconto_valor)
    valor_com_desconto = max(0, valor_original - desconto_valor)
    logger.info("Valor com desconto: %f", valor_com_desconto)

    # Montar e salvar item
    inscricao_id = str(uuid.uuid4())
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

    # Notificações
    try:
        enviar_email_para_aluno(item)
        enviar_email_para_admin(item)
    except Exception:
        logger.exception("Erro ao enviar e-mails de inscrição")

    return resposta(201, {"message":"Inscrição criada com sucesso!", "inscricao_id": inscricao_id})


def checa_cupom_e_retorna_desconto(cupom, curso):
    resp = table_descontos.scan(
        FilterExpression="cupom = :c AND curso = :s",
        ExpressionAttributeValues={":c":cupom, ":s":curso}
    )
    items = resp.get("Items", [])
    return items[0].get("desconto") if items else None


def criar_paymentlink_asaas(curso, aluno, cpf, valor, metodo, ext_ref):
    hdr = {"Content-Type":"application/json", "access_token":ASAAS_API_KEY}
    nome = f"Inscrição: {curso}"
    desc = f"{nome}. Aluno: {aluno} - CPF: {cpf}"
    if metodo == "PIX":
        payload = {
            "name": nome, "billingType": "PIX", "chargeType": "DETACHED",
            "value": valor, "description": desc,
            "dueDateLimitDays": 2, "externalReference": ext_ref,
            "notificationEnabled": True
        }
    else:
        tc = round(valor * 1.08, 2)
        payload = {
            "name": nome, "billingType": "CREDIT_CARD", "chargeType": "INSTALLMENT",
            "value": tc, "description": desc,
            "dueDateLimitDays": 7, "maxInstallmentCount": 12,
            "externalReference": ext_ref, "notificationEnabled": True
        }
    logger.info("Asaas payload: %s", payload)
    resp = requests.post(f"{ASAAS_ENDPOINT}/paymentLinks", headers=hdr, json=payload)
    resp.raise_for_status()
    result = resp.json()
    logger.info("Asaas response: %s", result)
    return result


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
    assunto = f"Inscrição confirmada: {insc['curso']}"
    html = f"<p>Olá {insc['nomeCompleto']}, obrigado por se inscrever!</p>"
    ses.send_email(Source=REMETENTE,
                   Destination={"ToAddresses":[insc["email"]]},
                   Message={"Subject":{"Data":assunto},
                            "Body":{"Html":{"Data":html}}})
    logger.info("Email confirmação inscrição enviado a %s", insc["email"])


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


def validar_jwt(hdr):
    if not hdr or not hdr.startswith("Bearer "):
        raise Exception("Invalid auth")
    token = hdr.split()[1]
    dec = auth.verify_id_token(token)
    logger.info("JWT validado: uid=%s email=%s", dec["uid"], dec.get("email"))
    return dec["uid"], dec.get("email")


def resposta(status, body):
    return {
        "statusCode": status,
        "headers": {
            "Access-Control-Allow-Origin":"*",
            "Access-Control-Allow-Headers":"*",
            "Access-Control-Allow-Methods":"OPTIONS,GET,POST,DELETE",
            "Content-Type":"application/json"
        },
        "body": json.dumps(body)
    }
