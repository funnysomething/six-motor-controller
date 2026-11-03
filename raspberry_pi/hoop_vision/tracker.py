"""Bounded, timestamp-aware image tracks with conservative public output gating."""

from dataclasses import dataclass
import math

import numpy as np

from .geometry import overlap
from .orientation import estimate_orientation


def mm(a, b):
    # Small fixed-size matrices: avoid BLAS thread overhead and platform-specific
    # floating-point status flags; no global NumPy warning suppression.
    return np.einsum("ij,jk->ik", a, b)


@dataclass(frozen=True)
class TrackerConfig:
    profile: str = "strict"
    acquire_seconds: float = .5
    acquire_observations: int = 5
    reacquire_seconds: float = .2
    reacquire_observations: int = 3
    max_gap_seconds: float = .3
    retain_seconds: float = 1.
    max_age_seconds: float = .2
    max_tracks: int = 8
    association_gate: float = 16.
    ambiguity_margin: float = 2.
    measurement_center_std: float = .02
    measurement_logsize_std: float = .12
    continuity_lock: bool = False
    lock_center_gate: float = .10
    lock_logsize_gate: float = .35
    lock_hold_seconds: float = .8
    maintenance_seconds: float = .6

    def __post_init__(self):
        if self.profile not in ("strict","ellipse"):
            raise ValueError("Unknown tracker profile")
        for name in ("acquire_seconds", "reacquire_seconds", "max_gap_seconds", "retain_seconds",
                     "max_age_seconds", "association_gate", "ambiguity_margin",
                     "measurement_center_std", "measurement_logsize_std", "lock_center_gate",
                     "lock_logsize_gate", "lock_hold_seconds", "maintenance_seconds"):
            if not math.isfinite(getattr(self, name)) or getattr(self, name) <= 0:
                raise ValueError(f"{name} must be finite and positive")
        for name in ("acquire_observations", "reacquire_observations", "max_tracks"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.retain_seconds <= self.max_gap_seconds:
            raise ValueError("Retention must exceed the prediction horizon")
        if type(self.continuity_lock) is not bool:
            raise ValueError("continuity_lock must be boolean")
        if self.continuity_lock and self.retain_seconds <= self.lock_hold_seconds:
            raise ValueError("Retention must exceed the local lock hold time")

    @classmethod
    def ellipse(cls, **overrides):
        values=dict(profile="ellipse",acquire_seconds=.1,acquire_observations=2,
                    reacquire_seconds=.05,reacquire_observations=2,
                    ambiguity_margin=.5,measurement_center_std=.035,measurement_logsize_std=.18,
                    continuity_lock=True,retain_seconds=2.)
        values.update(overrides)
        return cls(**values)


def measurement(candidate, size):
    w, h = size
    x0, y0, x1, y1 = candidate.bbox_xyxy
    return np.array(((x0+x1)/(2*w), (y0+y1)/(2*h),
                     math.log((x1-x0)/w), math.log((y1-y0)/h)))


def state_box(x, size):
    w, h = size
    bw, bh = np.exp(np.clip(x[2:4], -10, 2))*[w, h]
    return [float(x[0]*w-bw/2), float(x[1]*h-bh/2),
            float(x[0]*w+bw/2), float(x[1]*h+bh/2)]


class HoopTracker:
    def __init__(self, config=None):
        self.config = config or TrackerConfig()
        self.next_id = 1
        self.reset()

    def reset(self):
        self.tracks = []
        self.last_time = None
        self.size = None
        self.selected_id = None

    def target_at(self, observation, now):
        """Consumption-time expiry, even if the camera stops producing frames.

        `now` must use the same clock domain as the capture timestamp. This
        returns image geometry only; it never grants navigation validity.
        """
        target = observation.accepted_target
        if observation.decision != "accepted" or target is None or not math.isfinite(now):
            return None
        timestamp = target.get("observed_time_s")
        if (timestamp != self.last_time or target["track_id"] != self.selected_id or
                now < timestamp or now-timestamp > self.config.max_age_seconds):
            return None
        return target

    def _new(self, c, timestamp, size):
        track = dict(id=self.next_id, kind=c.kind, x=np.r_[measurement(c, size), np.zeros(4)],
                     p=np.diag([.02**2]*2+[.1**2]*2+[.2**2]*2+[.3**2]*2),
                     observed=timestamp, history=[timestamp], state="tentative",
                     ever_confirmed=False, candidate=c,last_candidate=c,
                     last_strong=timestamp,strong_candidate=c,boundary_history=1)
        self.next_id += 1
        self.tracks.append(track)
        return track

    def _noise(self, c):
        quality = c.verification.get("oriented_coverage", .7)
        return np.diag([self.config.measurement_center_std**2]*2+
                       [self.config.measurement_logsize_std**2]*2)*(1+4*(1-quality))

    def search_horizon(self, track):
        if (self.config.profile == 'ellipse' and self.config.continuity_lock and
                track['ever_confirmed'] and track['id'] == self.selected_id):
            return self.config.lock_hold_seconds
        return self.config.max_gap_seconds

    def _lock_cost(self, track, candidate, size):
        """Slow-motion prior in image coordinates, independent of proposal rank."""
        old = track['last_candidate']
        if candidate.clipped:
            return math.inf
        center = np.array(candidate.center_px)
        previous = np.array(old.center_px)
        distance = float(np.linalg.norm((center-previous)/size))
        axes = np.asarray(sorted(candidate.axes_px), dtype=float)
        previous_axes = np.asarray(sorted(old.axes_px), dtype=float)
        if np.any(axes <= 0) or np.any(previous_axes <= 0):
            return math.inf
        shape = float(np.max(np.abs(np.log(axes/previous_axes))))
        if distance > self.config.lock_center_gate:
            return math.inf
        boundary_change = shape > self.config.lock_logsize_gate
        if boundary_change and not (shape <= .7 and candidate.verification.get('passed') is True and
                candidate.verification.get('strong') is True and
                self._same_target_boxes(old.bbox_xyxy,candidate.bbox_xyxy)):
            return math.inf
        # Near circles have no stable major-axis direction. Penalize angle
        # changes less as the ellipse approaches a circle.
        angle = lambda c: (c.image_rotation_deg + (90 if c.axes_px[0]<c.axes_px[1] else 0)) % 180
        rotation = abs((angle(candidate)-angle(old)+90) % 180-90)/90
        eccentricity = 1-min(old.axes_px)/max(old.axes_px)
        quality = candidate.verification.get('quality_score', 0.)
        return (distance/self.config.lock_center_gate + .6*shape/self.config.lock_logsize_gate +
                .3*rotation*eccentricity + .2*(candidate.kind != old.kind) - .05*quality +
                2.*boundary_change)

    def _can_maintain(self, track, candidate, timestamp):
        check = candidate.verification or {}
        if track is None:
            return False
        # Weak matches cannot walk the lock away by taking many small steps.
        reference=track['strong_candidate']
        drift=float(np.linalg.norm((np.array(candidate.center_px)-reference.center_px)/self.size))
        axes=np.asarray(sorted(candidate.axes_px),dtype=float)
        reference_axes=np.asarray(sorted(reference.axes_px),dtype=float)
        if np.any(axes<=0) or np.any(reference_axes<=0):
            return False
        shape=float(np.max(np.abs(np.log(axes/reference_axes))))
        return (track['state'] in ('confirmed','coasting') and drift<=self.config.lock_center_gate and
                shape<=self.config.lock_logsize_gate and
                timestamp-track['last_strong'] <= self.config.maintenance_seconds + 1e-9 and
                check.get('maintenance_passed') is True and check.get('white_color_passed') is True and
                self._lock_cost(track,candidate,self.size) < math.inf)

    def _predict(self, track, dt):
        f = np.eye(8)
        f[:4, 4:] = np.eye(4)*dt
        g = np.vstack((np.eye(4)*dt*dt/2, np.eye(4)*dt))
        q = mm(mm(g, np.diag([.3**2]*2+[.6**2]*2)), g.T)
        track["x"] = np.einsum("ij,j->i",f,track["x"])
        track["p"] = mm(mm(f,track["p"]),f.T)+q

    def _cost(self, track, c, size):
        if track["kind"] != c.kind:
            if self.config.profile!="ellipse" or not self._same_target_boxes(state_box(track['x'],size),c.bbox_xyxy):
                return math.inf
            # Different boundary models have different extents and centers.
            # Associate the object using position only, then reset its geometry
            # covariance before correction instead of treating scale change as motion.
            delta=measurement(c,size)[:2]-track['x'][:2]
            s=track['p'][:2,:2]+self._noise(c)[:2,:2]
            return float(delta@np.linalg.solve(s,delta))+2.
        z = measurement(c, size)
        innovation = z-track["x"][:4]
        if np.linalg.norm(innovation[:2]) > .2 or np.max(np.abs(innovation[2:])) > .6:
            return math.inf
        if overlap(state_box(track["x"], size), c.bbox_xyxy) < .1:
            return math.inf
        s = track["p"][:4, :4]+self._noise(c)
        return float(innovation @ np.linalg.solve(s, innovation)) + (
            3.*(self.last_time-track["observed"])/self.config.max_gap_seconds
            if self.config.profile == "ellipse" else 0.)

    @staticmethod
    def _same_object(a, b):
        # Used only to identify spatially duplicate evidence, never to average
        # different measurement kinds into a common filter state.
        return overlap(a.bbox_xyxy, b.bbox_xyxy) >= .55

    @staticmethod
    def _same_target_boxes(a,b):
        ca=np.array([(a[0]+a[2])/2,(a[1]+a[3])/2])
        cb=np.array([(b[0]+b[2])/2,(b[1]+b[3])/2])
        extent=max(a[2]-a[0],a[3]-a[1],b[2]-b[0],b[3]-b[1])
        intersection=max(0.,min(a[2],b[2])-max(a[0],b[0]))*max(0.,min(a[3],b[3])-max(a[1],b[1]))
        areas=[max(1e-9,(box[2]-box[0])*(box[3]-box[1])) for box in (a,b)]
        nested=intersection/min(areas)>=.85 and min(areas)/max(areas)>=.15
        return (overlap(a,b)>=.25 or nested) and np.linalg.norm(ca-cb)<=.3*extent

    def update(self, observation, timestamp, provenance="decoder_pts", age_seconds=0.):
        cfg = self.config
        observation.schema_version = 2
        observation.decision = "unknown"
        observation.accepted_target = None
        observation.image_orientation = None
        observation.tracks = []
        reason = "accumulating_evidence"
        if (len(observation.frame_size) != 2 or
                any(type(v) is not int or v <= 0 for v in observation.frame_size)):
            self.reset()
            observation.tracking_reason = "invalid_frame_size"
            return observation
        if (timestamp is None or not math.isfinite(timestamp) or timestamp < 0 or
                provenance not in ("decoder_pts", "avfoundation_pts") or not math.isfinite(age_seconds) or age_seconds < 0):
            self.reset()
            observation.tracking_reason = "untrusted_timestamp"
            return observation
        if self.last_time is not None and timestamp <= self.last_time:
            self.reset()
            observation.tracking_reason = "timestamp_discontinuity"
            return observation
        if self.size is not None and tuple(observation.frame_size) != self.size:
            self.reset()
            observation.tracking_reason = "coordinate_system_changed"
            return observation
        self.size = tuple(observation.frame_size)
        dt = 0. if self.last_time is None else timestamp-self.last_time
        self.last_time = timestamp
        self.tracks = [t for t in self.tracks if timestamp-t["observed"] < cfg.retain_seconds]
        if self.selected_id not in [t["id"] for t in self.tracks]:
            self.selected_id = None
        for track in self.tracks:
            gap = timestamp-track["observed"]
            track["candidate"] = None
            if gap > self.search_horizon(track)+1e-9:
                track["state"] = "lost"
                track["history"] = []
            else:
                self._predict(track, dt)
        eligible = []
        anchor = next((t for t in self.tracks if cfg.profile=='ellipse' and cfg.continuity_lock and
                       t['id']==self.selected_id and t['ever_confirmed'] and t['state']!='lost' and
                       timestamp-t['observed'] <= cfg.lock_hold_seconds+1e-9), None)
        aperture_conflict = False
        for candidate in observation.candidates:
            check = candidate.verification or {}
            values = (*candidate.bbox_xyxy, *candidate.center_px, *candidate.axes_px,
                      candidate.image_rotation_deg, candidate.score,check.get('quality_score',0.))
            quality = check.get("oriented_coverage",float("nan"))
            if (not math.isfinite(quality) or not 0 <= quality <= 1 or
                    not all(math.isfinite(v) for v in values) or
                    min(candidate.axes_px) <= 0 or
                    candidate.bbox_xyxy[2] <= candidate.bbox_xyxy[0] or
                    candidate.bbox_xyxy[3] <= candidate.bbox_xyxy[1]):
                continue
            if check.get('passed') is not True and not self._can_maintain(anchor,candidate,timestamp):
                continue
            if cfg.profile=='strict' and any(candidate.kind == c.kind and self._same_object(candidate, c) for c in eligible):
                continue
            eligible.append(candidate)
        if cfg.profile == "ellipse":
            if anchor is not None:
                local = [c for c in eligible if self._lock_cost(anchor,c,self.size) < math.inf and
                         c.verification.get('quality_score',0) >= .5 and
                         (c.verification.get('passed') is True or self._can_maintain(anchor,c,timestamp))]
                # Commit to one local measurement before assignment. Nested
                # outline/opening tracks must not alternate just on edge score.
                eligible = [min(local,key=lambda c:self._lock_cost(anchor,c,self.size))] if local else []
            apertures=[c for c in eligible if c.kind=='apparent_opening' and
                       c.verification.get('strong') and c.verification.get('quality_score',0)>=.7]
            aperture_conflict=any(not self._same_target_boxes(a.bbox_xyxy,b.bbox_xyxy)
                                  for i,a in enumerate(apertures) for b in apertures[i+1:])
            eligible=[c for c in eligible if c.verification.get("quality_score",0)>=(.5 if anchor else .6)]
            def priority(c):
                continuity=any(t['ever_confirmed'] and t['id']==self.selected_id and
                               timestamp-t['observed']<=cfg.max_gap_seconds and
                               overlap(state_box(t['x'],self.size),c.bbox_xyxy)>=.55
                               for t in self.tracks)
                return c.verification.get('quality_score',0)+(.12 if continuity else 0.)
            eligible.sort(key=priority,reverse=True)
            legacy_anchor=next((t for t in self.tracks if t['id']==self.selected_id and
                         t['ever_confirmed'] and t['state']!='lost'),None)
            if anchor is None and legacy_anchor is not None and eligible:
                locked=[c for c in eligible if self._cost(legacy_anchor,c,self.size)<=cfg.association_gate and
                        self._same_target_boxes(state_box(legacy_anchor['x'],self.size),c.bbox_xyxy)]
                if locked and priority(locked[0])>=priority(eligible[0])-.15:
                    # Another ring elsewhere is not an identity switch while
                    # the original track still has a compatible current edge fit.
                    eligible=[c for c in eligible if self._same_target_boxes(c.bbox_xyxy,locked[0].bbox_xyxy)]
                    aperture_conflict=False
            unique=[]
            for c in eligible:
                if not any(c.kind==other.kind and self._same_target_boxes(c.bbox_xyxy,other.bbox_xyxy) for other in unique):
                    unique.append(c)
            eligible=unique
            if eligible:
                best=eligible[0]
                # Competing weak outlines do not veto a well-supported target.
                # Keep genuinely comparable, spatially distinct targets ambiguous.
                eligible=[c for c in eligible if self._same_target_boxes(c.bbox_xyxy,best.bbox_xyxy) or
                          priority(c) >= priority(best)-.07]
        # Any distinct eligible object makes selection ambiguous, even before
        # it has existed long enough to confirm a competing track.
        same_target = (lambda a,b:self._same_target_boxes(a.bbox_xyxy,b.bbox_xyxy)) if cfg.profile=='ellipse' else self._same_object
        scene_ambiguous = aperture_conflict or any(not same_target(a, b)
                              for i, a in enumerate(eligible) for b in eligible[i+1:])
        pairs = []
        for i, track in enumerate(self.tracks):
            if track["state"] == "lost":
                continue
            for j, candidate in enumerate(eligible):
                # The explicit local center/shape gate is the association test
                # for the held identity. A lagging box covariance must not
                # discard a qualified current fit inside that neighborhood.
                cost = (self._lock_cost(track,candidate,self.size) if track is anchor
                        else self._cost(track,candidate,self.size))
                if cost <= cfg.association_gate:
                    pairs.append((cost, i, j))
        ambiguous_tracks, ambiguous_candidates = set(), set()
        for cost, i, j in pairs:
            for other, oi, oj in pairs:
                if cfg.profile=='ellipse':
                    if i==oi and same_target(eligible[j],eligible[oj]):continue
                    if j==oj and self._same_target_boxes(state_box(self.tracks[i]['x'],self.size),state_box(self.tracks[oi]['x'],self.size)):continue
                if ((i == oi and j != oj) or (j == oj and i != oi)) and abs(cost-other) < cfg.ambiguity_margin:
                    ambiguous_tracks.update((i, oi))
                    ambiguous_candidates.update((j, oj))
        used_tracks, used_candidates = set(), set()
        current = []
        for cost, i, j in sorted(pairs,key=lambda p:(0 if anchor is not None and self.tracks[p[1]] is anchor else 1,p[0])):
            if i in used_tracks or j in used_candidates or i in ambiguous_tracks or j in ambiguous_candidates:
                continue
            track, candidate = self.tracks[i], eligible[j]
            previous = track['last_candidate']
            shape_change = float(np.max(np.abs(np.log(np.sort(candidate.axes_px)/np.sort(previous.axes_px)))))
            same_boundary = candidate.kind==previous.kind and shape_change <= cfg.lock_logsize_gate
            track['boundary_history'] = track.get('boundary_history',1)+1 if same_boundary else 0
            if track['kind']!=candidate.kind:
                # Preserve object-level history, but restart the geometry filter
                # instead of averaging centers from different boundary models.
                track['x'][:4]=measurement(candidate,self.size)
                track['kind']=candidate.kind
                # Cross-covariances also belong to the old measurement model.
                # Replacing only the position block can make P indefinite.
                track['p']=np.diag([.02**2]*2+[.1**2]*2+[.2**2]*2+[.3**2]*2)
            r = self._noise(candidate)
            k = np.linalg.solve(track["p"][:4, :4]+r, track["p"][:4, :]).T
            track["x"] += k @ (measurement(candidate, self.size)-track["x"][:4])
            residual = np.eye(8)
            residual[:, :4] -= k
            track["p"] = mm(mm(residual,track["p"]),residual.T)+mm(mm(k,r),k.T)
            track["observed"] = timestamp
            track["candidate"] = candidate
            track["last_candidate"] = candidate
            if (age_seconds <= cfg.max_age_seconds and candidate.verification.get('passed') and
                    candidate.verification.get('strong')):
                track['last_strong'] = timestamp
                track['strong_candidate'] = candidate
            used_tracks.add(i)
            used_candidates.add(j)
            current.append(track)
        for i, track in enumerate(self.tracks):
            if i not in used_tracks:
                track["history"] = []
                if track["state"] != "lost":
                    track["state"] = "coasting"
        for j, candidate in enumerate(eligible):
            if (j not in used_candidates and j not in ambiguous_candidates and
                    candidate.verification.get("strong") and len(self.tracks) < cfg.max_tracks):
                current.append(self._new(candidate, timestamp, self.size))
        for track in current:
            if scene_ambiguous or ambiguous_tracks or age_seconds > cfg.max_age_seconds:
                track["history"] = []
                track["state"] = "coasting" if track["ever_confirmed"] else "tentative"
                continue
            # Starting a track requires strong evidence. A held local identity
            # may reconfirm using bounded, weaker current measurements.
            local_reconfirmation = (track is anchor and self._can_maintain(track,track['candidate'],timestamp))
            if (not track["candidate"].verification.get("strong") and track["state"] != "confirmed"
                    and not local_reconfirmation):
                track["history"] = []
                continue
            if not track["history"] or track["history"][-1] != timestamp:
                track["history"].append(timestamp)
            track["history"] = [t for t in track["history"] if timestamp-t <= 2.]
            reacquiring = track["ever_confirmed"] and track["state"] != "lost"
            count = cfg.reacquire_observations if reacquiring else cfg.acquire_observations
            duration = cfg.reacquire_seconds if reacquiring else cfg.acquire_seconds
            if (len(track["history"]) >= count and timestamp-track["history"][0]+1e-9 >= duration):
                track["state"] = "confirmed"
                track["ever_confirmed"] = True
        confirmed = [t for t in current if t["state"] == "confirmed"]
        if age_seconds > cfg.max_age_seconds:
            reason = "stale_measurement"
        elif scene_ambiguous or ambiguous_tracks:
            reason = "ambiguous_targets_or_association"
        elif not eligible:
            reason = "locked_target_not_observed" if anchor is not None else "no_verified_measurement"
        elif confirmed:
            choices = confirmed if cfg.profile=='ellipse' else [
                t for t in confirmed if self.selected_id is None or t['id']==self.selected_id]
            if choices:
                chosen = next((t for t in choices if anchor is not None and t['id']==anchor['id']),None)
                chosen = chosen or max(choices, key=lambda t: t["candidate"].verification.get("oriented_coverage", 0))
                self.selected_id = chosen["id"]
                observation.decision = "accepted"
                observation.accepted_target = dict(track_id=chosen["id"], kind=chosen["kind"],
                    ellipse=dict(center_px=list(chosen["candidate"].center_px),
                                 axes_px=list(chosen["candidate"].axes_px),
                                 image_rotation_deg=chosen["candidate"].image_rotation_deg),
                    bbox_xyxy=list(chosen["candidate"].bbox_xyxy),
                    filtered_bbox_xyxy=state_box(chosen["x"], self.size),
                    observed_time_s=timestamp, age_seconds=age_seconds,
                    evidence='verified' if chosen['candidate'].verification.get('passed') else 'local_maintenance',
                    boundary_stable=not cfg.continuity_lock or chosen['boundary_history']>=2)
                c=chosen['candidate']
                if observation.accepted_target['boundary_stable']:
                    observation.image_orientation=estimate_orientation((c.center_px,c.axes_px,c.image_rotation_deg),self.size)
                reason = ("maintained_local_image_track" if observation.accepted_target['evidence']=='local_maintenance'
                          else "fresh_confirmed_image_track")
                if not observation.accepted_target['boundary_stable']:
                    reason = 'tracked_boundary_unstable'
            else:
                reason = "previous_target_unresolved"
        for track in self.tracks:
            observation.tracks.append(dict(track_id=track["id"], kind=track["kind"], state=track["state"],
                predicted_bbox_xyxy=state_box(track["x"], self.size),
                last_observed_time_s=track["observed"], observed_this_frame=track["candidate"] is not None,
                preferred_local_track=bool(cfg.continuity_lock and track['id']==self.selected_id and track['state']!='lost'),
                position_uncertainty=float(np.sqrt(np.trace(track["p"][:2, :2])))))
        observation.tracking_reason = reason
        return observation
