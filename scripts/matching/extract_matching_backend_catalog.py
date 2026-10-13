"""Extract enum values and region rows from the backend source export."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import zipfile

ROOT = Path(__file__).resolve().parents[2]
PATHS = {
    'themes':'src/main/java/com/audigo/domain/travel/entity/TravelThemeType.java',
    'paces':'src/main/java/com/audigo/domain/travel/entity/TravelPaceType.java',
    'genders':'src/main/java/com/audigo/domain/matching/entity/MatchingGender.java',
    'preferred_genders':'src/main/java/com/audigo/domain/matching/entity/PreferredCompanionGender.java',
    'regions':'src/main/resources/data.sql',
}


def extract(archive_path):
    with zipfile.ZipFile(archive_path) as archive:
        first=next(name for name in archive.namelist() if name.endswith(PATHS['themes']))
        prefix=first[:-len(PATHS['themes'])]
        commit=archive.comment.decode().strip()
        if not re.fullmatch('[0-9a-f]{40}',commit):
            raise ValueError('Expected a GitHub source export containing its commit SHA')
        result={'source_commit':commit,'sources':{}}
        for key,path in PATHS.items():
            raw=archive.read(prefix+path)
            source=raw.decode()
            result['sources'][key]={'path':path,'sha256':hashlib.sha256(raw).hexdigest()}
            if key=='regions':
                result[key]=[dict(name=a,full_name=b) for a,b in re.findall(
                    r"\('([^']+)',\s*'([^']+)',\s*NOW\(\),\s*NOW\(\)\)",source)]
            else:
                result[key]=re.findall(r'\b[A-Z][A-Z_]+\b',source.split('{',1)[1].split('}',1)[0])
            if not result[key]:
                raise ValueError('No backend values extracted for '+key)
        if len({r['full_name'] for r in result['regions']})!=len(result['regions']):
            raise ValueError('Duplicate backend region full_name')
        return result


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--backend-zip',type=Path,required=True)
    parser.add_argument('--output',type=Path,default=ROOT/'ai_service/matching/data/matching_backend_catalog.json')
    args=parser.parse_args()
    data=extract(args.backend_zip)
    args.output.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'source_commit':data['source_commit'],
                      'counts':{key:len(data[key]) for key in PATHS}},ensure_ascii=False))
