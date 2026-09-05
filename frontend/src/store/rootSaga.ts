import { all } from 'redux-saga/effects'
import { jobsWsSaga } from './sagas/jobsWsSaga'

export function* rootSaga() {
  yield all([jobsWsSaga()])
}
