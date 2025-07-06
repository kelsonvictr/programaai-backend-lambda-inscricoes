import json
import boto3
import uuid
import os
import requests
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table('Inscricoes')

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
ASAAS_API_KEY = os.environ.get('ASAAS_API_KEY')
ASAAS_ENDPOINT = "https://www.asaas.com/api/v3"

def salvar_inscricao(event, context):
    logger.info("Evento recebido: %s", json.dumps(event))

    if event.get('httpMethod') == 'OPTIONS':
        return {
            'statusCode': 200,
            'headers': cors_headers(),
            'body': json.dumps({'message': 'CORS OK'})
        }

    try:
        body_raw = event.get('body')
        body = json.loads(body_raw)

        if body.get("website"):
            logger.warning("Tentativa de bot detectada.")
            return {
                'statusCode': 400,
                'headers': cors_headers(),
                'body': json.dumps({'error': 'Solicitação inválida.'})
            }

        agora = datetime.now(timezone(timedelta(hours=-3))).isoformat()
        ip = event.get('requestContext', {}).get('identity', {}).get('sourceIp', 'desconhecido')
        user_agent = event.get('headers', {}).get('User-Agent', 'desconhecido')

        inscricao_id = str(uuid.uuid4())
        external_ref = str(uuid.uuid4())

        valor_curso = float(body['valor'])
        payment_method = body.get('paymentMethod', 'PIX').upper()
        nome_curso = body['curso']
        nome_aluno = body['nomeCompleto']
        cpf_aluno = body.get('cpf', '')

        # cria link de pagamento no Asaas
        payment_link = criar_paymentlink_asaas(
            nome_curso, nome_aluno, cpf_aluno, valor_curso, payment_method, external_ref
        )

        item = {
            'id': inscricao_id,
            'curso': nome_curso,
            'nomeCompleto': nome_aluno,
            'email': body['email'],
            'cpf': cpf_aluno,
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
            enviar_para_telegram(item)
        except Exception as err:
            logger.error("Erro ao enviar para o Telegram: %s", err)

        return {
            'statusCode': 201,
            'headers': cors_headers(),
            'body': json.dumps({
                'message': 'Inscrição e link de pagamento criados com sucesso!',
                'linkPagamento': payment_link.get('url', '')
            })
        }

    except Exception as e:
        logger.error("Erro inesperado: %s", e, exc_info=True)
        return {
            'statusCode': 500,
            'headers': cors_headers(),
            'body': json.dumps({'error': str(e)})
        }


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
        valor_com_taxa = round(valor * 1.08, 2)  # +8%
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

    logger.info(f"Payload para Asaas: {payload}")

    response = requests.post(f"{ASAAS_ENDPOINT}/paymentLinks", headers=headers, json=payload)
    response.raise_for_status()
    return response.json()


def enviar_para_telegram(inscricao):
    mensagem = f"""
📩 Nova inscrição recebida!

📚 Curso: {inscricao['curso']}
👤 Nome: {inscricao['nomeCompleto']}
📧 Email: {inscricao['email']}
📱 WhatsApp: {inscricao['whatsapp']}
🆔 CPF: {inscricao.get('cpf', '')}
⚧ Sexo: {inscricao['sexo']}
🎂 Nascimento: {inscricao['dataNascimento']}
🎓 Formação TI: {inscricao['formacaoTI']}
🏫 Onde estuda: {inscricao.get('ondeEstuda', '')}
📣 Como soube: {inscricao['comoSoube']}
👥 Amigo: {inscricao.get('nomeAmigo', '')}
🛡️ Aceitou os termos: Sim
🕒 Data: {inscricao['dataInscricao']}
💳 Método: {inscricao['paymentMethod']}
🔗 Link pagamento: {inscricao['asaasPaymentLinkUrl']}
🖥️ IP / Navegador: {inscricao['ip']} / {inscricao['userAgent']}
""".strip()

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    response = requests.post(url, json={
        'chat_id': TELEGRAM_CHAT_ID,
        'text': mensagem
    })
    logger.info("Resposta do Telegram: %s", response.text)


def cors_headers():
    return {
        'Access-Control-Allow-Origin': 'https://programaai.dev',
        'Access-Control-Allow-Headers': '*',
        'Access-Control-Allow-Methods': 'OPTIONS,POST',
        'Content-Type': 'application/json'
    }
