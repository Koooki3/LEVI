// Making the job file for a wizard run, once per distinct form. The server
// never overwrites a job (a taken name is 409 job_exists), so a form that did
// not change reuses the job already made, a changed form makes a new job, and
// a taken name is retried with a new suffix.
import { ApiError, newRequestId, type WizardApi } from "./wizard-api";
import { buildJob, jobKey, jobName, type JobDraft } from "./wizard-logic";

const MAX_NAME_TRIES = 3;

export class JobAttempts {
  private key = "";
  private requestId = "";
  private name = "";
  private jobId: string | null = null;
  private tries = 0;

  /** The id of the job for `draft`: made now, or the one made before for the
   * same draft. Throws the server's ApiError (422 carries field errors). */
  async ensure(
    api: Pick<WizardApi, "createJob">,
    draft: JobDraft,
    now: Date = new Date(),
  ): Promise<string> {
    const key = jobKey(draft);
    if (key !== this.key) {
      this.key = key;
      this.jobId = null;
      this.tries = 0;
      this.requestId = newRequestId();
      this.name = jobName(draft.task.instruction, now);
    }
    if (this.jobId) return this.jobId;
    for (;;) {
      try {
        const { id } = await api.createJob(
          buildJob(draft, this.requestId, this.name),
        );
        this.jobId = id;
        return id;
      } catch (error) {
        if (
          error instanceof ApiError &&
          error.is("job_exists") &&
          this.tries < MAX_NAME_TRIES
        ) {
          this.tries += 1;
          this.requestId = newRequestId();
          this.name = jobName(
            draft.task.instruction,
            now,
            Math.random().toString(36).slice(2, 6),
          );
          continue;
        }
        throw error;
      }
    }
  }

  /** Forget the job (the next ensure makes a new one). */
  reset() {
    this.key = "";
    this.jobId = null;
  }
}
