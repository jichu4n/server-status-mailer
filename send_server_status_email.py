#!/usr/bin/env python3
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                                                                             #
#    Copyright (C) 2015 Chuan Ji <ji@chu4n.com>                               #
#                                                                             #
#    Licensed under the Apache License, Version 2.0 (the "License");          #
#    you may not use this file except in compliance with the License.         #
#    You may obtain a copy of the License at                                  #
#                                                                             #
#     http://www.apache.org/licenses/LICENSE-2.0                              #
#                                                                             #
#    Unless required by applicable law or agreed to in writing, software      #
#    distributed under the License is distributed on an "AS IS" BASIS,        #
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. #
#    See the License for the specific language governing permissions and      #
#    limitations under the License.                                           #
#                                                                             #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

import argparse
import concurrent.futures
import datetime
import email.mime.application
import email.mime.multipart
import email.mime.text
import io
import jinja2
import logging
import multiprocessing
import os
import slugify
import smtplib
import socket
import subprocess
import types
import yaml
import zipfile


CommandResult = types.SimpleNamespace


def ValidateConfig(config):
  config.setdefault('smtp', {})
  config['smtp'].setdefault('server', 'localhost')
  config['smtp'].setdefault('login', False)
  if config['smtp'].get('login'):
    assert 'user' in config['smtp'], (
        '\'user\' attribute must be specified if SMTP requires login')
    assert 'password' in config['smtp'], (
        '\'password\' attribute must be specified if SMTP requires login')
  config['smtp'].setdefault('tls', False)
  config['smtp'].setdefault('ssl', False)
  config['smtp'].setdefault('port', 0)

  assert 'from' in config, (
      'Missing \'from\' attribute')
  assert isinstance(config.get('to'), list) and config['to'], (
      'Missing or empty \'to\' attribute')
  config.setdefault('subject', 'Server status for {host}')

  config.setdefault('date_time_format', '%c')

  config.setdefault('attachment', {})
  config['attachment'].setdefault(
      'file_name', 'Server status for {host} [{now_str}].zip')
  config['attachment'].setdefault(
      'command_result_file_name', '{label_slug}.txt')

  assert isinstance(config.get('commands'), list), (
      'Missing \'commands\' attribute')
  for i, command in enumerate(config['commands']):
    assert 'label' in command, (
        'Command %d missing \'label\' attribute' % i)
    assert 'command' in command, (
        'Command %d missing \'command\' attribute' % i)
    command.setdefault('attachment_only', False)
    command.setdefault(
        'label_slug', slugify.slugify(command['label']))


def RunCommand(command_dict):
  result = CommandResult(**command_dict)
  result.start_time = datetime.datetime.now()

  if not result.command:
    result.stderr = (
        'Error: No command specified for label \'%s\'' % result.label)
    result.stdout = ''
    result.returncode = None
    return result

  logging.info('Running command %s: %s', result.label, result.command)
  p = subprocess.Popen(
      result.command,
      shell=True,
      stdout=subprocess.PIPE,
      stderr=subprocess.PIPE)
  stdout, stderr = p.communicate()
  result.stdout = stdout.decode('utf-8')
  result.stderr = stderr.decode('utf-8')
  result.returncode = p.returncode

  result.end_time = datetime.datetime.now()
  logging.info('Completed command %s: %s', result.label, result.command)
  return result


if __name__ == '__main__':
  # Set up logging.
  logging.basicConfig(
      level=logging.DEBUG,
      style='{',
      format='{levelname:.1}{asctime} {filename}:{lineno}] {message}')

  logging.info('Starting %s...', os.path.abspath(__file__))

  script_dir = os.path.dirname(os.path.abspath(__file__))

  # Parse args.
  parser = argparse.ArgumentParser(
      formatter_class=argparse.ArgumentDefaultsHelpFormatter)
  parser.add_argument(
      '-f', '--config-file',
      help='Path to a YAML config file.',
      default=os.path.join(script_dir, 'config.yml'))
  parser.add_argument(
      '--body-template',
      help='Path to a Jinja2 template file for the email body',
      default=os.path.join(script_dir, 'body-template.html'))
  parser.add_argument(
      '--attachment-template',
      help='Path to a Jinja2 template file for command result attachment files',
      default=os.path.join(script_dir, 'attachment-template.txt'))
  try:
    num_cpus = multiprocessing.cpu_count() * 2
  except NotImplementedError:
    num_cpus = 1
  parser.add_argument(
      '--num-parallel-commands',
      help='Maximum number of commands to run in parallel',
      type=int,
      default=num_cpus)
  args = vars(parser.parse_args())

  # Load config.
  logging.info('Loading config from %s', args['config_file'])
  with open(args['config_file'], 'r') as config_file:
    config = yaml.load(config_file)
  logging.info('Loaded config:\n%s', yaml.dump(config))
  ValidateConfig(config)

  # Prepare template.
  logging.info('Loading body template from %s', args['body_template'])
  with open(args['body_template'], 'r') as body_template_file:
    body_template = jinja2.Template(
        body_template_file.read())
  logging.info(
      'Loading attachment template from %s', args['attachment_template'])
  with open(args['attachment_template'], 'r') as attachment_template_file:
    attachment_template = jinja2.Template(
        attachment_template_file.read())

  # Run commands and gather results.
  with concurrent.futures.ThreadPoolExecutor(
      max_workers=args['num_parallel_commands']) as executor:
    command_results = list(executor.map(RunCommand, config['commands']))
  for command_result in command_results:
    command_result.start_time_str = command_result.start_time.strftime(
        config['date_time_format'])
    command_result.end_time_str = command_result.end_time.strftime(
        config['date_time_format'])
    command_result.execution_time_seconds = (
        command_result.end_time - command_result.start_time).total_seconds()

  logging.info('Generating email body')
  now = datetime.datetime.now()
  context = {
      'host': socket.gethostname(),
      'now': now,
      'now_str': now.strftime(config['date_time_format']),
      'command_results': command_results,
      'attachment_only_command_results': [
          command_result
          for command_result in command_results
          if command_result.attachment_only
      ],
      'args': args,
      'config': config,
  }
  message_body = email.mime.text.MIMEText(
      body_template.render(context).strip(), 'html')
  logging.info('Generated email body:\n%s', message_body.as_string())

  logging.info('Generating attachment')
  raw_file = io.BytesIO()
  with zipfile.ZipFile(raw_file, 'w') as zip_file:
    for command_result in command_results:
      command_result_context = context.copy()
      command_result_context['command_result'] = command_result
      command_result_context.update(vars(command_result))
      command_result_file_name = (
          config['attachment']['command_result_file_name'].format(
            **command_result_context))
      logging.info('Creating and adding %s', command_result_file_name)
      zip_file.writestr(
          command_result_file_name,
          attachment_template.render(command_result_context).strip())
  attachment = email.mime.application.MIMEApplication(
      raw_file.getvalue(), 'zip')
  attachment_file_name = config['attachment']['file_name'].format(
      **command_result_context)
  attachment['Content-Type'] = (
      'application/zip; name=%s' % attachment_file_name)
  attachment['Content-Disposition'] = (
      'attachment; filename=%s' % attachment_file_name)

  message = email.mime.multipart.MIMEMultipart()
  message['From'] = config['from']
  message['To'] = ', '.join(config['to'])
  message['Subject'] = config['subject'].format(**context)
  message.attach(message_body)
  message.attach(attachment)

  logging.info(
      'Sending email to %s via %s on port %d',
      ', '.join(config['to']),
      config['smtp']['server'],
      config['smtp']['port'])
  if config['smtp']['ssl']:
    logging.info('Connecting via SSL')
    smtp_conn = smtplib.SMTP_SSL(
        config['smtp']['server'],
        config['smtp']['port'])
  else:
    smtp_conn = smtplib.SMTP(
        config['smtp']['server'],
        config['smtp']['port'])
    if config['smtp']['tls']:
      logging.info('Enabling TLS')
      smtp_conn.starttls()
  if config['smtp']['login']:
    logging.info(
        'Logging in to %s as %s',
        config['smtp']['server'],
        config['smtp']['user'])
    smtp_conn.login(config['smtp']['user'], config['smtp']['password'])
  logging.info('Transferring message')
  smtp_conn.send_message(message)

  smtp_conn.quit()

  logging.info('Done.')
