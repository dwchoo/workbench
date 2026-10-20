"""Small, fully consumed credential-free OMP model-config migration subset.

This is not a general YAML loader or a secret scanner. Unknown syntax/fields
require manual configuration at the new location; rejected content is not echoed.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from .paths import DataDirError

_LIMIT = 128 * 1024
_KEY = re.compile(r'[A-Za-z][A-Za-z0-9_-]{0,63}\Z')
_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z')


def _unsupported():
    raise ValueError('unsupported configuration')


def _scalar(text):
    if text == 'true':return True
    if text == 'false':return False
    if re.fullmatch(r'0|[1-9][0-9]{0,9}',text):return int(text)
    if text.startswith('"'):
        value=json.loads(text)
        if not isinstance(value,str):_unsupported()
        return value
    if (not text or text in ('---','...') or any(c in text for c in "[]{}#!&*|>'\"`\\")
            or ': ' in text or text[0] in '-?@:'):_unsupported()
    return text


def _yaml_subset(text):
    tokens=[]
    for line in text.splitlines():
        if not line.strip():continue
        if '\t' in line or line.rstrip()!=line or line.lstrip().startswith('#'):_unsupported()
        indent=len(line)-len(line.lstrip(' '))
        if indent%2 or indent>16:_unsupported()
        tokens.append((indent,line[indent:]))
    if len(tokens)>4096 or not tokens or tokens[0][0]!=0:_unsupported()

    def pair(content):
        key,sep,value=content.partition(':')
        if not sep or not _KEY.fullmatch(key) or (value and not value.startswith(' ')):_unsupported()
        return key,value.strip()

    def assign(result,content,index,indent):
        key,value=pair(content)
        if key in result:_unsupported()
        if value:
            result[key]=_scalar(value)
        else:
            if index>=len(tokens) or tokens[index][0]!=indent+2:_unsupported()
            result[key],index=block(index,indent+2)
        return index

    def block(index,indent):
        if tokens[index][1].startswith('- '):
            result=[]
            while index<len(tokens) and tokens[index][0]==indent and tokens[index][1].startswith('- '):
                row={};content=tokens[index][1][2:];index+=1
                index=assign(row,content,index,indent+2)
                while index<len(tokens) and tokens[index][0]==indent+2:
                    content=tokens[index][1];index+=1
                    index=assign(row,content,index,indent+2)
                result.append(row)
            return result,index
        result={}
        while index<len(tokens) and tokens[index][0]==indent:
            content=tokens[index][1];index+=1
            index=assign(result,content,index,indent)
        return result,index

    value,end=block(0,0)
    if end!=len(tokens):_unsupported()
    return value


def _object(value,required,optional=()):
    if not isinstance(value,dict) or not set(required)<=set(value) or set(value)-set(required)-set(optional):_unsupported()


def _text(value,limit=256):
    if not isinstance(value,str) or not 0<len(value)<=limit or any(ord(c)<32 or ord(c)==127 for c in value):_unsupported()


def _model_config(value):
    _object(value,('providers',),('startup',))
    if 'startup' in value:
        _object(value['startup'],('setupWizard',))
        if value['startup']['setupWizard'] is not False:_unsupported()
    providers=value['providers']
    if not isinstance(providers,dict) or not 1<=len(providers)<=64:_unsupported()
    for name,provider in providers.items():
        if not isinstance(name,str) or not _NAME.fullmatch(name):_unsupported()
        _object(provider,('baseUrl','api','auth','models'))
        url=provider['baseUrl'];_text(url,2048)
        parsed=urlsplit(url)
        if (parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment or '?' in url or '#' in url
                or '%' in parsed.netloc or '\\' in url or any(c.isspace() for c in url)
                or not re.fullmatch(r'/[A-Za-z0-9/._~-]*|',parsed.path)):_unsupported()
        _=parsed.port  # malformed/out-of-range ports are unsupported too.
        if provider['auth']!='none' or provider['api'] not in ('openai-completions','anthropic-messages','google-generative-ai'):_unsupported()
        models=provider['models']
        if not isinstance(models,list) or not 1<=len(models)<=256:_unsupported()
        ids=set()
        for model in models:
            _object(model,('id',),('name','contextWindow','maxTokens'))
            _text(model['id'])
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9/._:-]{0,255}',model['id']) or model['id'] in ids:_unsupported()
            ids.add(model['id'])
            if 'name' in model:_text(model['name'])
            for key in ('contextWindow','maxTokens'):
                if key in model and (type(model[key]) is not int or not 0<model[key]<=2**31-1):_unsupported()


def validate_model_bytes(content):
    if not 0<len(content)<=_LIMIT:_unsupported()
    _model_config(_yaml_subset(content.decode('utf-8')))


def _read_config(source_fd,key,item):
    import os
    import stat
    from hashlib import sha256
    if item['kind']!='file' or not 0<item['size']<=_LIMIT:_unsupported()
    parent=os.dup(source_fd)
    try:
        for part in Path(key).parts[:-1]:
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=parent)
            os.close(parent);parent=child
        fd=os.open(Path(key).name,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=parent)
        try:
            info=os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=os.geteuid()
                    or (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns)!=(item['device'],item['inode'],item['size'],item['mtime_ns'])):_unsupported()
            content=bytearray()
            while len(content)<=_LIMIT:
                chunk=os.read(fd,_LIMIT+1-len(content))
                if not chunk:break
                content.extend(chunk)
            if sha256(content).hexdigest()!=item['sha256']:_unsupported()
            return bytes(content)
        finally:os.close(fd)
    finally:os.close(parent)


def _generated_config(content):
    from .launcher import ISOLATION_PROVIDER_IDS
    from .omp_home import _config_text,home_config
    # Only the exact known producer format may be archived/rebuilt. No general
    # YAML loading, arbitrary keys, headers or credentials are carried through.
    header=_config_text({}).split(b'{',1)[0]
    if not content.startswith(header):_unsupported()
    value=json.loads(content[len(header):])
    directory=value['skills']['customDirectories']
    if not isinstance(directory,list) or len(directory)!=1:_unsupported()
    _text(directory[0],4096)
    if not Path(directory[0]).is_absolute() or '://' in directory[0]:_unsupported()
    if content!=_config_text(home_config(directory[0],ISOLATION_PROVIDER_IDS)):_unsupported()


def validate_source_configs(source_fd,entries):
    """Bounded no-follow validation before any target config/history copy.

    Recognized custom models are active durable config. Only exact generated
    WB config is archived. Unsupported input requires explicit manual setup.
    """
    for key,validate in (('omp-root/agent/models.yml',validate_model_bytes),
                         ('omp-root/agent/config.yml',_generated_config)):
        if key not in entries:continue
        try:validate(_read_config(source_fd,key,entries[key]))
        except (ValueError,UnicodeError,RecursionError,TypeError,KeyError,IndexError):
            raise DataDirError(f'unsupported credential-free configuration at {key}; configure models manually before migration; no configuration was copied') from None
