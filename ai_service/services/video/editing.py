"""생성 영상의 선형 겹침 구간만 찾아 제거한다. 새 사진/프레임은 만들지 않는다."""
import numpy as np
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FilmTake:
    """A reviewed range from generated video, at the original frame rate."""
    source: Path
    start_frame: int
    frame_count: int
    memory_role: str
    crop: tuple[int,int,int,int] | None = None


def find_overlap_ranges(frames, references, guide_indices, *, max_remove_fraction=0.35):
    frames=np.asarray(frames,dtype=np.float32)/255
    references=np.asarray(references,dtype=np.float32)/255
    mask=np.zeros(len(frames),dtype=bool)
    for index,image in enumerate(frames):
        interval=min(max(int(np.searchsorted(guide_indices,index,side='right'))-1,0),len(references)-2)
        first,last=references[interval],references[interval+1]
        delta=first-last
        alpha=float(np.clip(np.sum((image-last)*delta)/max(float(np.sum(delta*delta)),1e-6),0,1))
        residual=float(np.mean((image-(alpha*first+(1-alpha)*last))**2))
        nearest=min(float(np.mean((image-first)**2)),float(np.mean((image-last)**2)))
        mask[index]=0.05<alpha<0.95 and residual<0.003 and nearest>0.001
    runs=[];start=None
    for index,value in enumerate(np.append(mask,False)):
        if value and start is None:start=index
        if not value and start is not None:
            if index-start>=3:runs.append((max(0,start-2),min(len(mask)-1,index+1)))
            start=None
    mask[:]=False
    for first,last in runs:mask[first:last+1]=True
    for guide in guide_indices:mask[max(0,guide-1):min(len(mask),guide+2)]=False
    ranges=[];start=None
    for index,value in enumerate(np.append(mask,False)):
        if value and start is None:start=index
        if not value and start is not None:ranges.append([start,index-1]);start=None
    too_much=int(mask.sum())>len(mask)*max_remove_fraction
    retained=np.flatnonzero(~mask)
    return {'algorithm':'linear_reference_overlap_v1','applied':bool(ranges) and not too_much,
            'status':'review_needed' if too_much else 'cuts_ready' if ranges else 'unchanged',
            'source_frames':len(mask),'removed_frames':int(mask.sum()) if not too_much else 0,
            'ranges_frames':ranges if not too_much else [],
            'retained_keyframe_indices':[int(np.searchsorted(retained,g)) for g in guide_indices] if not too_much else guide_indices,
            'max_remove_fraction':max_remove_fraction,
            'note':'Heuristic linear blend detection. Endpoints/keyframes are protected. Generated motion with a large residual is retained.'}
