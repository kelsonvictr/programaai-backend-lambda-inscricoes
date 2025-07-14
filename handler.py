import json
import boto3
import uuid
import os
import requests
import logging
from datetime import datetime, timedelta, timezone

import firebase_admin
from firebase_admin import auth, credentials

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table('Inscricoes')
ses = boto3.client('ses')
s3 = boto3.client('s3')

ASAAS_API_KEY = os.environ.get('ASAAS')
ASAAS_ENDPOINT = "https://www.asaas.com/api/v3"
REMETENTE = 'Programa AI <no-reply@programaai.dev>'

FIREBASE_BUCKET = os.environ.get('FIREBASE_BUCKET')
FIREBASE_KEY_PATH = os.environ.get('FIREBASE_KEY_PATH')
ADMIN_EMAIL = os.environ.get('ADMIN_EMAIL')

def init_firebase():
    if not firebase_admin._apps:
        logger.info(f"Carregando chave Firebase do bucket {FIREBASE_BUCKET}/{FIREBASE_KEY_PATH}")
        obj = s3.get_object(Bucket=FIREBASE_BUCKET, Key=FIREBASE_KEY_PATH)
        json_key = json.load(obj['Body'])
        cred = credentials.Certificate(json_key)
        firebase_admin.initialize_app(cred)

init_firebase()

def salvar_inscricao(event, context):
    logger.info("Evento recebido: %s", json.dumps(event))

    path = event.get("path", "")
    method = event.get("httpMethod", "")
    logger.info(f"Path recebido: {path} | Method: {method}")

    if "/galaxy" in path:
        try:
            auth_header = event["headers"].get("Authorization", "")
            uid, email = validar_jwt(auth_header)
            logger.info(f"Usuário autenticado: {email} ({uid})")
        except Exception as e:
            logger.error(f"Falha na autenticação: {e}")
            return resposta(401, {'error': 'Unauthorized'})

        if path.endswith("/galaxy/inscricoes") and method == "GET":
            return listar_inscricoes()

        if path.startswith("/galaxy/inscricoes/") and method == "DELETE":
            inscricao_id = path.split("/")[-1]
            return remover_inscricao(inscricao_id)

        return resposta(404, {'error': 'Admin route not found'})

    if method == 'OPTIONS':
        return resposta(200, {'message': 'CORS OK'})

    if path.endswith("/inscricao") and method == "POST":
        try:
            body_raw = event.get('body')
            if not body_raw:
                return resposta(400, {'error': 'Body is required'})

            body = json.loads(body_raw)

            if body.get("website"):
                logger.warning("Tentativa de bot detectada.")
                return resposta(400, {'error': 'Solicitação inválida.'})

            cpf_aluno = body.get('cpf', '')
            nome_curso = body['curso']

            # Verificar duplicidade
            if verificar_inscricao_existente(cpf_aluno, nome_curso):
                return resposta(409, {
                    'error': f"O aluno com CPF {cpf_aluno} já está inscrito no curso {nome_curso}."
                })

            agora = datetime.now(timezone(timedelta(hours=-3))).isoformat()
            ip = event.get('requestContext', {}).get('identity', {}).get('sourceIp', 'desconhecido')
            user_agent = event.get('headers', {}).get('User-Agent', 'desconhecido')

            inscricao_id = str(uuid.uuid4())
            external_ref = str(uuid.uuid4())

            valor_curso = float(body['valor'])
            payment_method = body.get('paymentMethod', 'PIX').upper()
            nome_aluno = body['nomeCompleto']
            rg_aluno = body.get('rg', '')

            payment_link = criar_paymentlink_asaas(
                nome_curso, nome_aluno, cpf_aluno, valor_curso, payment_method, external_ref
            )

            item = {
                'id': inscricao_id,
                'curso': nome_curso,
                'nomeCompleto': nome_aluno,
                'cpf': cpf_aluno,
                'rg': rg_aluno,
                'email': body['email'],
                'whatsapp': body['whatsapp'],
                'sexo': body['sexo'],
                'dataNascimento': body['dataNascimento'],
                'formacaoTI': body['formacaoTI'],
                'ondeEstuda': body.get('ondeEstuda', ''),
                'comoSoube': body['comoSoube'],
                'nomeAmigo': body.get('nomeAmigo', ''),
                'dataInscricao': agora,
                'aceitouTermos': True,
                'ip': ip,
                'userAgent': user_agent,
                'asaasPaymentLinkId': payment_link.get('id', ''),
                'asaasPaymentLinkUrl': payment_link.get('url', ''),
                'asaasExternalReference': external_ref,
                'paymentMethod': payment_method
            }

            table.put_item(Item=item)
            logger.info("Item salvo no DynamoDB e paymentLink criado")

            try:
                enviar_email_para_aluno(item)
                enviar_email_para_admin(item)
            except Exception as err:
                logger.error(f"Falha ao enviar e-mails: {err}")

            return resposta(201, {
                'message': 'Inscrição e link de pagamento criados com sucesso!',
                'linkPagamento': payment_link.get('url', '')
            })

        except Exception as e:
            logger.error("Erro inesperado: %s", e, exc_info=True)
            return resposta(500, {'error': str(e)})

    return resposta(404, {'error': 'Route not found'})

def verificar_inscricao_existente(cpf, curso):
    """
    Retorna True se já existe uma inscrição para o mesmo CPF + curso.
    """
    try:
        response = table.scan(
            FilterExpression='cpf = :cpf_val AND curso = :curso_val',
            ExpressionAttributeValues={
                ':cpf_val': cpf,
                ':curso_val': curso
            }
        )
        items = response.get('Items', [])
        if items:
            logger.info(f"Inscrição já existente para CPF {cpf} no curso {curso}")
            return True
        return False
    except Exception as e:
        logger.error(f"Erro ao verificar duplicidade: {e}", exc_info=True)
        return False

def listar_inscricoes():
    try:
        result = table.scan()
        logger.info(f"Total inscrições encontradas: {len(result.get('Items', []))}")
        return resposta(200, result.get("Items", []))
    except Exception as e:
        logger.error("Erro ao listar: %s", e, exc_info=True)
        return resposta(500, {'error': str(e)})

def remover_inscricao(inscricao_id):
    try:
        table.delete_item(Key={'id': inscricao_id})
        logger.info(f"Inscrição {inscricao_id} removida")
        return resposta(200, {'message': 'Inscrição removida com sucesso'})
    except Exception as e:
        logger.error("Erro ao remover: %s", e, exc_info=True)
        return resposta(500, {'error': str(e)})

def criar_paymentlink_asaas(curso, aluno, cpf, valor, metodo, external_ref):
    headers = {
        "Content-Type": "application/json",
        "access_token": ASAAS_API_KEY
    }

    nome = f"Inscrição: {curso}"
    descricao = f"Inscrição: {curso}. Aluno(a): {aluno} - CPF: {cpf}"

    if metodo == 'PIX':
        payload = {
            "name": nome,
            "billingType": "PIX",
            "chargeType": "DETACHED",
            "value": valor,
            "description": descricao,
            "dueDateLimitDays": 2,
            "externalReference": external_ref,
            "notificationEnabled": True
        }
    elif metodo == 'CARTAO':
        valor_com_taxa = round(valor * 1.08, 2)
        payload = {
            "name": nome,
            "billingType": "CREDIT_CARD",
            "chargeType": "INSTALLMENT",
            "value": valor_com_taxa,
            "description": descricao,
            "dueDateLimitDays": 7,
            "maxInstallmentCount": 12,
            "externalReference": external_ref,
            "notificationEnabled": True
        }
    else:
        raise ValueError(f"Método de pagamento inválido: {metodo}")

    response = requests.post(f"{ASAAS_ENDPOINT}/paymentLinks", headers=headers, json=payload)
    response.raise_for_status()
    return response.json()

def validar_jwt(authorization_header):
    if not authorization_header:
        raise Exception("Authorization header missing")
    parts = authorization_header.split()
    if len(parts) != 2 or parts[0] != "Bearer":
        raise Exception("Invalid authorization header")

    token = parts[1]
    decoded_token = auth.verify_id_token(token)
    uid = decoded_token['uid']
    email = decoded_token.get('email')
    return uid, email

def enviar_email_para_aluno(inscricao):
    logger.info(f"Enviando e-mail para aluno {inscricao['email']}")

    assunto = f"Inscrição confirmada: {inscricao['curso']}"
    corpo_html = f"""
    <div style="font-family: Arial, sans-serif; color: #333;">
      <img src="https://programaai.dev/assets/logo-BPg_3cKF.png" alt="Programa AI" style="height: 50px; margin-bottom: 20px;" />
      <h2>Inscrição confirmada 🎉</h2>
      <p>Olá <strong>{inscricao['nomeCompleto']}</strong>, obrigado por se inscrever no curso <strong>{inscricao['curso']}</strong>!</p>
      <p>Para confirmar sua inscrição, por favor realize o pagamento clicando no link abaixo:</p>
      <p><a href="{inscricao['asaasPaymentLinkUrl']}" style="background-color: #4CAF50; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px;">Clique aqui para pagar</a></p>
      <p>Após a confirmação do pagamento, entraremos em contato e adicionaremos você ao nosso grupo no WhatsApp onde soltamos todas as novidades do curso.</p>
      <br/>
      <p>Equipe <strong>Programa AI</strong></p>
    </div>
    """

    ses.send_email(
        Source=REMETENTE,
        Destination={'ToAddresses': [inscricao['email']]},
        Message={
            'Subject': {'Data': assunto},
            'Body': {'Html': {'Data': corpo_html}}
        }
    )

def enviar_email_para_admin(inscricao):
    logger.info("Enviando e-mail para admin")

    assunto = f"Nova inscrição: {inscricao['curso']} - {inscricao['nomeCompleto']}"
    corpo_html = f"""
    <div style="font-family: Arial, sans-serif; color: #333;">
      <img src="https://programaai.dev/assets/logo-BPg_3cKF.png" alt="Programa AI" style="height: 50px; margin-bottom: 20px;" />
      <h2>Nova inscrição recebida</h2>
      <p><strong>Curso:</strong> {inscricao['curso']}</p>
      <p><strong>Nome:</strong> {inscricao['nomeCompleto']}</p>
      <p><strong>E-mail:</strong> {inscricao['email']}</p>
      <p><strong>WhatsApp:</strong> {inscricao['whatsapp']}</p>
      <p><strong>CPF:</strong> {inscricao['cpf']}</p>
      <p><strong>RG:</strong> {inscricao['rg']}</p>
      <p><strong>Sexo:</strong> {inscricao['sexo']}</p>
      <p><strong>Data de nascimento:</strong> {inscricao['dataNascimento']}</p>
      <p><strong>Formação TI:</strong> {inscricao['formacaoTI']}</p>
      <p><strong>Onde estuda:</strong> {inscricao.get('ondeEstuda', '')}</p>
      <p><strong>Como soube:</strong> {inscricao['comoSoube']}</p>
      <p><strong>Amigo:</strong> {inscricao.get('nomeAmigo', '')}</p>
      <p><strong>IP / UserAgent:</strong> {inscricao['ip']} / {inscricao['userAgent']}</p>
      <p><strong>Método de pagamento:</strong> {inscricao['paymentMethod']}</p>
      <p><strong>Link pagamento:</strong> <a href="{inscricao['asaasPaymentLinkUrl']}">{inscricao['asaasPaymentLinkUrl']}</a></p>
      <br/>
      <p>Equipe <strong>Programa AI</strong></p>
    </div>
    """

    ses.send_email(
        Source=REMETENTE,
        Destination={'ToAddresses': [ADMIN_EMAIL]},
        Message={
            'Subject': {'Data': assunto},
            'Body': {'Html': {'Data': corpo_html}}
        }
    )

def resposta(status, body):
    return {
        'statusCode': status,
        'headers': cors_headers(),
        'body': json.dumps(body)
    }

def cors_headers():
    return {
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Headers': '*',
        'Access-Control-Allow-Methods': 'OPTIONS,GET,POST,DELETE',
        'Content-Type': 'application/json'
    }
