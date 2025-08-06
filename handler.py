import json
import os
import uuid
import logging
from datetime import datetime, timedelta, timezone

import boto3
import requests
import firebase_admin
from firebase_admin import auth, credentials

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# AWS clients & resources
dynamodb         = boto3.resource('dynamodb')
table_inscricoes = dynamodb.Table('Inscricoes')
table_interesse  = dynamodb.Table('ListaInteresse')
table_cursos     = dynamodb.Table('Cursos')
table_descontos  = dynamodb.Table('Descontos')
ses              = boto3.client('ses')
s3               = boto3.client('s3')

# Configs
ASAAS_API_KEY    = os.environ.get('ASAAS')
ASAAS_ENDPOINT   = "https://www.asaas.com/api/v3"
REMETENTE        = 'programa AI <no-reply@programaai.dev>'
FIREBASE_BUCKET  = os.environ.get('FIREBASE_BUCKET')
FIREBASE_KEY_PATH= os.environ.get('FIREBASE_KEY_PATH')
ADMIN_EMAIL      = os.environ.get('ADMIN_EMAIL')

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

    # POST /clube/interesse
    if path.endswith("/clube/interesse") and method == "POST":
        body = json.loads(event.get("body","{}"))
        if body.get("website"):
            return resposta(400, {"error":"Solicitação inválida."})
        nome, email, aceita = body.get("nome"), body.get("email"), body.get("aceitaContato")
        if not nome or not email or not aceita:
            return resposta(400, {"error":"Nome, email e aceitaContato são obrigatórios."})
        if verificar_interesse_existente(email):
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
        try:
            enviar_email_boas_vindas_clube(item)
            enviar_email_admin_clube(item)
        except Exception:
            logger.exception("Erro enviando e-mails clube")
        return resposta(201, {"message":"Cadastro no Clube realizado."})

    # GET /clube/interesse?email=...
    if path.endswith("/clube/interesse") and method == "GET":
        email = qs.get("email","").strip()
        if not email:
            return resposta(400, {"error":"Parâmetro 'email' é obrigatório."})
        existe = verificar_interesse_existente(email)
        return resposta(200, {"existe": existe})

    # GET /checa-cupom?cupom=XXX&curso=YYY
    if path.endswith("/checa-cupom") and method == "GET":
        cupom = qs.get("cupom","").strip()
        curso = qs.get("curso","").strip()
        if not cupom or not curso:
            return resposta(400, {"error":"Parâmetros 'cupom' e 'curso' são obrigatórios."})
        scan = table_descontos.scan(
            FilterExpression="cupom = :c AND curso = :u AND ativo = :t AND disponivel = :t",
            ExpressionAttributeValues={":c":cupom, ":u":curso, ":t":True}
        )
        valid = bool(scan.get("Items", []))
        return resposta(200, {"valid": valid})

    # POST /paymentlink
    if path.endswith("/paymentlink") and method == "POST":
        try:
            body = json.loads(event.get("body","{}"))
            iid  = body.get("inscricaoId","").strip()
            pm   = body.get("paymentMethod","PIX").upper()
            if not iid or pm not in ("PIX","CARTAO"):
                return resposta(400, {"error":"inscricaoId e paymentMethod válidos são obrigatórios."})
            resp = table_inscricoes.get_item(Key={"id": iid})
            insc = resp.get("Item")
            if not insc:
                return resposta(404, {"error":f"Inscrição '{iid}' não encontrada"})
            aluno = insc.get("nomeCompleto","")
            cpf   = insc.get("cpf","")
            curso = insc.get("curso","")
            # busca curso por nome
            scan = table_cursos.scan(
                FilterExpression="title = :t",
                ExpressionAttributeValues={":t":curso}
            ).get("Items",[])
            if not scan:
                return resposta(404, {"error":f"Curso '{curso}' não encontrado"})
            raw_price = scan[0].get("price","")
            clean = raw_price.replace("R$","").replace(" ","").replace(".","").replace(",",".")
            valor = float(clean)
            link = criar_paymentlink_asaas(curso, aluno, cpf, valor, pm, str(uuid.uuid4()))
            return resposta(200, {"inscricaoId": iid, "paymentLinkId": link.get("id"), "url": link.get("url")})
        except Exception as e:
            logger.exception("Erro ao gerar paymentlink")
            return resposta(500, {"error": str(e)})

    # GET /cursos or GET /cursos?id=...
    if path.endswith("/cursos") and method == "GET":
        cid = qs.get("id")
        try:
            if cid:
                r = table_cursos.get_item(Key={"id": cid}).get("Item")
                if not r: return resposta(404, {"error":f"Curso '{cid}' não encontrado"})
                return resposta(200, r)
            else:
                items = table_cursos.scan().get("Items",[])
                return resposta(200, items)
        except Exception as e:
            logger.exception("Erro listando cursos")
            return resposta(500, {"error":"Falha ao buscar cursos"})

    # POST /inscricao
    if path.endswith("/inscricao") and method == "POST":
        return processar_inscricao(event, context)

    # Admin routes (galaxy)
    if "/galaxy" in path:
        try:
            hdr = event["headers"].get("Authorization","")
            uid, email = validar_jwt(hdr)
        except Exception:
            return resposta(401, {"error":"Unauthorized"})
        if path.endswith("/galaxy/inscricoes") and method == "GET":
            return listar_inscricoes()
        if path.startswith("/galaxy/inscricoes/") and method == "DELETE":
            iid = path.split("/")[-1]
            return remover_inscricao(iid)
        return resposta(404, {"error":"Admin route not found"})

    # CORS preflight
    if method == "OPTIONS":
        return resposta(200, {"message":"CORS OK"})

    return resposta(404, {"error":"Route not found"})


# inscrição sem link Asaas, com cupom
def processar_inscricao(event, context):
    try:
        body = json.loads(event.get("body","{}"))
        if body.get("website"):
            return resposta(400, {"error":"Solicitação inválida."})

        # dados básicos
        nome_curso  = body["curso"]
        nome_aluno  = body["nomeCompleto"]
        cpf_aluno   = body.get("cpf","")
        rg_aluno    = body.get("rg","")
        email       = body["email"]
        whatsapp    = body["whatsapp"]
        sexo        = body["sexo"]
        dnasc       = body["dataNascimento"]
        formacao    = body["formacaoTI"]
        onde        = body.get("ondeEstuda","")
        como_soube  = body["comoSoube"]
        amigo       = body.get("nomeAmigo","")
        cupom_user  = body.get("cupom","").strip()

        # valor original e descontado
        raw_val    = body["valor"]
        vo         = float(raw_val.replace("R$","").replace(".","").replace(",","."))
        vd         = vo

        # aplica cupom
        if cupom_user:
            scan = table_descontos.scan(
                FilterExpression="cupom = :c AND curso = :u AND ativo = :t AND disponivel = :t",
                ExpressionAttributeValues={":c":cupom_user,":u":nome_curso,":t":True}
            ).get("Items",[])
            if scan:
                d = scan[0]["desconto"]
                if d.endswith("%"):
                    pct = float(d.rstrip("%").replace(",","."))/100
                    vd = round(vo*(1-pct),2)
                else:
                    fx = float(d.replace("R$","").replace(".","").replace(",","."))
                    vd = round(max(vo-fx,0),2)
            else:
                cupom_user = ""

        iid = str(uuid.uuid4())
        item = {
            "id": iid,
            "curso": nome_curso,
            "nomeCompleto": nome_aluno,
            "cpf": cpf_aluno,
            "rg": rg_aluno,
            "email": email,
            "whatsapp": whatsapp,
            "sexo": sexo,
            "dataNascimento": dnasc,
            "formacaoTI": formacao,
            "ondeEstuda": onde,
            "comoSoube": como_soube,
            "nomeAmigo": amigo,
            "dataInscricao": datetime.now(timezone(timedelta(hours=-3))).isoformat(),
            "aceitouTermos": True,
            "valorOriginal": str(vo),
            "valorComDesconto": str(vd),
            "cupom": cupom_user
        }
        table_inscricoes.put_item(Item=item)
        try:
            enviar_email_para_aluno(item)
            enviar_email_para_admin(item)
        except Exception:
            logger.exception("Erro enviando e-mails inscrição")
        return resposta(201, {"message":"Inscrição criada", "inscricaoId": iid})

    except Exception as e:
        logger.exception("Erro em processar_inscricao")
        return resposta(500, {"error":str(e)})


# geração de payment link Asaas
def criar_paymentlink_asaas(curso, aluno, cpf, valor, metodo, ext_ref):
    hdr = {"Content-Type":"application/json","access_token":ASAAS_API_KEY}
    nome = f"Inscrição: {curso}"
    desc = f"{nome}. Aluno: {aluno} - CPF: {cpf}"
    if metodo=="PIX":
        pl = {"name":nome, "billingType":"PIX", "chargeType":"DETACHED",
              "value":valor, "description":desc, "dueDateLimitDays":2,
              "externalReference":ext_ref, "notificationEnabled":True}
    else:
        tc = round(valor*1.08,2)
        pl = {"name":nome, "billingType":"CREDIT_CARD","chargeType":"INSTALLMENT",
              "value":tc, "description":desc, "dueDateLimitDays":7,
              "maxInstallmentCount":12, "externalReference":ext_ref,
              "notificationEnabled":True}
    r = requests.post(f"{ASAAS_ENDPOINT}/paymentLinks", headers=hdr, json=pl)
    r.raise_for_status()
    return r.json()


# helpers de e-mail, verificação, listagem e jwt
def enviar_email_boas_vindas_clube(item):
    assunto = "🎉 Bem-vindo ao Clube programa AI!"
    html = f"<h2>Parabéns {item['nome']}!</h2><p>Você entrou no Clube!</p>"
    ses.send_email(Source=REMETENTE,
                   Destination={"ToAddresses":[item["email"]]},
                   Message={"Subject":{"Data":assunto},
                            "Body":{"Html":{"Data":html}}})

def enviar_email_admin_clube(item):
    assunto = f"Novo membro do Clube: {item['nome']}"
    html = f"<p>Email: {item['email']}</p>"
    ses.send_email(Source=REMETENTE,
                   Destination={"ToAddresses":[ADMIN_EMAIL]},
                   Message={"Subject":{"Data":assunto},
                            "Body":{"Html":{"Data":html}}})

def enviar_email_para_aluno(insc):
    assunto = f"Inscrição confirmada: {insc['curso']}"
    html = f"<p>Olá {insc['nomeCompleto']}, obrigado por se inscrever!</p>"
    ses.send_email(Source=REMETENTE,
                   Destination={"ToAddresses":[insc["email"]]},
                   Message={"Subject":{"Data":assunto},
                            "Body":{"Html":{"Data":html}}})

def enviar_email_para_admin(inscricao):
    assunto = f"📥 Nova inscrição: {inscricao['curso']} - {inscricao['nomeCompleto']}"
    # Monta um HTML simples listando cada campo da inscrição
    html = f"""
    <div style="font-family: Arial, sans-serif; color: #333;">
      <h2>Nova inscrição recebida</h2>
      <ul>
        <li><strong>ID:</strong> {inscricao.get('id','')}</li>
        <li><strong>Curso:</strong> {inscricao.get('curso','')}</li>
        <li><strong>Nome completo:</strong> {inscricao.get('nomeCompleto','')}</li>
        <li><strong>CPF:</strong> {inscricao.get('cpf','')}</li>
        <li><strong>RG:</strong> {inscricao.get('rg','')}</li>
        <li><strong>E-mail:</strong> {inscricao.get('email','')}</li>
        <li><strong>WhatsApp:</strong> {inscricao.get('whatsapp','')}</li>
        <li><strong>Sexo:</strong> {inscricao.get('sexo','')}</li>
        <li><strong>Data de Nascimento:</strong> {inscricao.get('dataNascimento','')}</li>
        <li><strong>Formação TI:</strong> {inscricao.get('formacaoTI','')}</li>
        <li><strong>Onde estuda/estudou:</strong> {inscricao.get('ondeEstuda','')}</li>
        <li><strong>Como soube:</strong> {inscricao.get('comoSoube','')}</li>
        <li><strong>Nome do amigo (indicação):</strong> {inscricao.get('nomeAmigo','')}</li>
        <li><strong>Data da inscrição:</strong> {inscricao.get('dataInscricao','')}</li>
        <li><strong>Valor original:</strong> {inscricao.get('valorOriginal','')}</li>
        <li><strong>Valor com desconto:</strong> {inscricao.get('valorComDesconto','')}</li>
        <li><strong>Cupom aplicado:</strong> {inscricao.get('cupom','')}</li>
      </ul>
    </div>
    """

    ses.send_email(
        Source=REMETENTE,
        Destination={"ToAddresses": [ADMIN_EMAIL]},
        Message={
            "Subject": {"Data": assunto},
            "Body": {"Html": {"Data": html}}
        }
    )


def verificar_inscricao_existente(cpf, curso):
    resp = table_inscricoes.scan(
        FilterExpression="cpf = :c AND curso = :u",
        ExpressionAttributeValues={":c":cpf,":u":curso}
    )
    return bool(resp.get("Items",[]))

def verificar_interesse_existente(email):
    resp = table_interesse.scan(
        FilterExpression="email = :e",
        ExpressionAttributeValues={":e":email}
    )
    return bool(resp.get("Items",[]))

def listar_inscricoes():
    items = table_inscricoes.scan().get("Items",[])
    return resposta(200, items)

def remover_inscricao(iid):
    table_inscricoes.delete_item(Key={"id":iid})
    return resposta(200, {"message":"Inscrição removida"})

def validar_jwt(hdr):
    if not hdr or not hdr.startswith("Bearer "):
        raise Exception("Invalid auth")
    token = hdr.split()[1]
    dec = auth.verify_id_token(token)
    return dec["uid"], dec.get("email")

def resposta(status, body):
    return {"statusCode":status, "headers":cors_headers(), "body":json.dumps(body)}

def cors_headers():
    return {
        "Access-Control-Allow-Origin":"*",
        "Access-Control-Allow-Headers":"*",
        "Access-Control-Allow-Methods":"OPTIONS,GET,POST,DELETE",
        "Content-Type":"application/json"
    }
